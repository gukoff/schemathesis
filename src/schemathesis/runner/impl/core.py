import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Union

import attr
import hypothesis
import requests
from _pytest.logging import LogCaptureHandler, catching_logs
from requests.auth import HTTPDigestAuth, _basic_auth_str

from ...constants import DEFAULT_DEADLINE, DEFAULT_STATEFUL_RECURSION_LIMIT, USER_AGENT, DataGenerationMethod
from ...exceptions import CheckFailed, InvalidSchema, get_grouped_exception
from ...hooks import HookContext, get_all_by_name
from ...models import Case, Check, CheckFunction, Endpoint, Status, TestResult, TestResultSet
from ...runner import events
from ...schemas import BaseSchema
from ...stateful import Feedback, Stateful
from ...targets import Target, TargetContext
from ...types import RawAuth
from ...utils import GenericResponse, WSGIResponse, capture_hypothesis_output, format_exception


def get_hypothesis_settings(hypothesis_options: Dict[str, Any]) -> hypothesis.settings:
    # Default settings, used as a parent settings object below
    hypothesis_options.setdefault("deadline", DEFAULT_DEADLINE)
    return hypothesis.settings(**hypothesis_options)


@attr.s  # pragma: no mutate
class BaseRunner:
    schema: BaseSchema = attr.ib()  # pragma: no mutate
    checks: Iterable[CheckFunction] = attr.ib()  # pragma: no mutate
    max_response_time: Optional[int] = attr.ib()  # pragma: no mutate
    targets: Iterable[Target] = attr.ib()  # pragma: no mutate
    hypothesis_settings: hypothesis.settings = attr.ib(converter=get_hypothesis_settings)  # pragma: no mutate
    auth: Optional[RawAuth] = attr.ib(default=None)  # pragma: no mutate
    auth_type: Optional[str] = attr.ib(default=None)  # pragma: no mutate
    headers: Optional[Dict[str, Any]] = attr.ib(default=None)  # pragma: no mutate
    request_timeout: Optional[int] = attr.ib(default=None)  # pragma: no mutate
    store_interactions: bool = attr.ib(default=False)  # pragma: no mutate
    seed: Optional[int] = attr.ib(default=None)  # pragma: no mutate
    exit_first: bool = attr.ib(default=False)  # pragma: no mutate
    stateful: Optional[Stateful] = attr.ib(default=None)  # pragma: no mutate
    stateful_recursion_limit: int = attr.ib(default=DEFAULT_STATEFUL_RECURSION_LIMIT)  # pragma: no mutate

    def execute(self) -> Generator[events.ExecutionEvent, None, None]:
        """Common logic for all runners."""
        results = TestResultSet()

        initialized = events.Initialized.from_schema(schema=self.schema)
        yield initialized

        for event in self._execute(results):
            if (
                self.exit_first
                and isinstance(event, events.AfterExecution)
                and event.status in (Status.error, Status.failure)
            ):
                break
            yield event

        yield events.Finished.from_results(results=results, running_time=time.monotonic() - initialized.start_time)

    def _execute(self, results: TestResultSet) -> Generator[events.ExecutionEvent, None, None]:
        raise NotImplementedError

    def _run_tests(
        self,
        maker: Callable,
        template: Callable,
        settings: hypothesis.settings,
        seed: Optional[int],
        recursion_level: int = 0,
        **kwargs: Any,
    ) -> Generator[events.ExecutionEvent, None, None]:
        """Run tests and recursively run additional tests."""
        if recursion_level > self.stateful_recursion_limit:
            return
        for endpoint, data_generation_method, test in maker(template, settings, seed):
            feedback = Feedback(self.stateful, endpoint)
            for event in run_test(
                endpoint,
                test,
                feedback=feedback,
                recursion_level=recursion_level,
                data_generation_method=data_generation_method,
                **kwargs,
            ):
                yield event
                if isinstance(event, events.Interrupted):
                    return
            # Additional tests, generated via the `feedback` instance
            yield from self._run_tests(
                feedback.get_stateful_tests, template, settings, seed, recursion_level=recursion_level + 1, **kwargs
            )


def run_test(  # pylint: disable=too-many-locals
    endpoint: Endpoint,
    test: Union[Callable, InvalidSchema],
    checks: Iterable[CheckFunction],
    data_generation_method: DataGenerationMethod,
    targets: Iterable[Target],
    results: TestResultSet,
    headers: Optional[Dict[str, Any]],
    recursion_level: int,
    **kwargs: Any,
) -> Generator[events.ExecutionEvent, None, None]:
    """A single test run with all error handling needed."""
    result = TestResult(endpoint=endpoint, overridden_headers=headers, data_generation_method=data_generation_method)
    yield events.BeforeExecution.from_endpoint(endpoint=endpoint, recursion_level=recursion_level)
    hypothesis_output: List[str] = []
    test_start_time = time.monotonic()
    try:
        if isinstance(test, InvalidSchema):
            status = Status.error
            result.add_error(test)
        else:
            with capture_hypothesis_output() as hypothesis_output:
                test(checks, targets, result, headers=headers, **kwargs)
            status = Status.success
    except (CheckFailed, hypothesis.errors.MultipleFailures):
        status = Status.failure
    except hypothesis.errors.Flaky:
        status = Status.error
        result.mark_errored()
        # Sometimes Hypothesis detects inconsistent test results and checks are not available
        if result.checks:
            flaky_example = result.checks[-1].example
        else:
            flaky_example = None
        result.add_error(
            hypothesis.errors.Flaky(
                "Tests on this endpoint produce unreliable results: \n"
                "Falsified on the first call but did not on a subsequent one"
            ),
            flaky_example,
        )
    except hypothesis.errors.Unsatisfiable:
        # We need more clear error message here
        status = Status.error
        result.add_error(hypothesis.errors.Unsatisfiable("Unable to satisfy schema parameters for this endpoint"))
    except KeyboardInterrupt:
        yield events.Interrupted()
        return
    except AssertionError as exc:  # comes from `hypothesis-jsonschema`
        error = reraise(exc)
        status = Status.error
        result.add_error(error)
    except Exception as error:
        status = Status.error
        result.add_error(error)
    test_elapsed_time = time.monotonic() - test_start_time
    # Fetch seed value, hypothesis generates it during test execution
    # It may be `None` if the `derandomize` config option is set to `True`
    result.seed = getattr(test, "_hypothesis_internal_use_seed", None) or getattr(
        test, "_hypothesis_internal_use_generated_seed", None
    )
    results.append(result)
    yield events.AfterExecution.from_result(
        result=result,
        status=status,
        elapsed_time=test_elapsed_time,
        hypothesis_output=hypothesis_output,
        endpoint=endpoint,
    )


def reraise(error: AssertionError) -> InvalidSchema:
    traceback = format_exception(error, True)
    if "assert type_ in TYPE_STRINGS" in traceback:
        message = "Invalid type name"
    else:
        message = "Unknown schema error"
    try:
        raise InvalidSchema(message) from error
    except InvalidSchema as exc:
        return exc


def run_checks(
    case: Case,
    checks: Iterable[CheckFunction],
    check_results: List[Check],
    result: TestResult,
    response: GenericResponse,
    elapsed_time: float,
    max_response_time: Optional[int] = None,
) -> None:
    errors = []

    for check in checks:
        check_name = check.__name__
        try:
            skip_check = check(response, case)
            if not skip_check:
                result.add_success(check_name, case)
                check_results.append(Check(check_name, Status.success, case))
        except AssertionError as exc:
            message = str(exc)
            if not message:
                message = f"Check '{check_name}' failed"
                exc.args = (message,)
            errors.append(exc)
            result.add_failure(check_name, case, message)
            check_results.append(Check(check_name, Status.failure, case, message))

    if max_response_time:
        if elapsed_time > max_response_time:
            message = f"Response time exceeded the limit of {max_response_time} ms"
            errors.append(AssertionError(message))
            result.add_failure("max_response_time", case, message)
        else:
            result.add_success("max_response_time", case)

    if errors:
        raise get_grouped_exception(case.endpoint.verbose_name, *errors)


def run_targets(targets: Iterable[Callable], context: TargetContext) -> None:
    for target in targets:
        value = target(context)
        hypothesis.target(value, label=target.__name__)


def add_cases(case: Case, response: GenericResponse, test: Callable, *args: Any) -> None:
    context = HookContext(case.endpoint)
    for case_hook in get_all_by_name("add_case"):
        _case = case_hook(context, case.partial_deepcopy(), response)
        # run additional test if _case is not an empty value
        if _case:
            test(_case, *args)


def network_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    session: requests.Session,
    request_timeout: Optional[int],
    request_tls_verify: bool,
    store_interactions: bool,
    headers: Optional[Dict[str, Any]],
    feedback: Feedback,
    max_response_time: Optional[int],
) -> None:
    """A single test body will be executed against the target."""
    headers = headers or {}
    if "user-agent" not in {header.lower() for header in headers}:
        headers["User-Agent"] = USER_AGENT
    timeout = prepare_timeout(request_timeout)
    response = _network_test(
        case,
        checks,
        targets,
        result,
        session,
        timeout,
        store_interactions,
        headers,
        feedback,
        request_tls_verify,
        max_response_time,
    )
    add_cases(
        case,
        response,
        _network_test,
        checks,
        targets,
        result,
        session,
        timeout,
        store_interactions,
        headers,
        feedback,
        request_tls_verify,
        max_response_time,
    )


def _network_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    session: requests.Session,
    timeout: Optional[float],
    store_interactions: bool,
    headers: Optional[Dict[str, Any]],
    feedback: Feedback,
    request_tls_verify: bool,
    max_response_time: Optional[int],
) -> requests.Response:
    response = case.call(session=session, headers=headers, timeout=timeout, verify=request_tls_verify)
    context = TargetContext(case=case, response=response, response_time=response.elapsed.total_seconds())
    run_targets(targets, context)
    status = Status.success
    check_results: List[Check] = []
    try:
        run_checks(case, checks, check_results, result, response, context.response_time * 1000, max_response_time)
    except CheckFailed:
        status = Status.failure
        raise
    finally:
        if store_interactions:
            result.store_requests_response(response, status, check_results)
    feedback.add_test_case(case, response)
    return response


@contextmanager
def get_session(auth: Optional[Union[HTTPDigestAuth, RawAuth]] = None) -> Generator[requests.Session, None, None]:
    with requests.Session() as session:
        if auth is not None:
            session.auth = auth
        yield session


def prepare_timeout(timeout: Optional[int]) -> Optional[float]:
    """Request timeout is in milliseconds, but `requests` uses seconds."""
    output: Optional[Union[int, float]] = timeout
    if timeout is not None:
        output = timeout / 1000
    return output


def wsgi_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    auth: Optional[RawAuth],
    auth_type: Optional[str],
    headers: Optional[Dict[str, Any]],
    store_interactions: bool,
    feedback: Feedback,
    max_response_time: Optional[int],
) -> None:
    headers = _prepare_wsgi_headers(headers, auth, auth_type)
    response = _wsgi_test(case, checks, targets, result, headers, store_interactions, feedback, max_response_time)
    add_cases(
        case, response, _wsgi_test, checks, targets, result, headers, store_interactions, feedback, max_response_time
    )


def _wsgi_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    headers: Dict[str, Any],
    store_interactions: bool,
    feedback: Feedback,
    max_response_time: Optional[int],
) -> WSGIResponse:
    with catching_logs(LogCaptureHandler(), level=logging.DEBUG) as recorded:
        start = time.monotonic()
        response = case.call_wsgi(headers=headers)
        elapsed = time.monotonic() - start
    context = TargetContext(case=case, response=response, response_time=elapsed)
    run_targets(targets, context)
    result.logs.extend(recorded.records)
    status = Status.success
    check_results: List[Check] = []
    try:
        run_checks(case, checks, check_results, result, response, context.response_time * 1000, max_response_time)
    except CheckFailed:
        status = Status.failure
        raise
    finally:
        if store_interactions:
            result.store_wsgi_response(case, response, headers, elapsed, status, check_results)
    feedback.add_test_case(case, response)
    return response


def _prepare_wsgi_headers(
    headers: Optional[Dict[str, Any]], auth: Optional[RawAuth], auth_type: Optional[str]
) -> Dict[str, Any]:
    headers = headers or {}
    if "user-agent" not in {header.lower() for header in headers}:
        headers["User-Agent"] = USER_AGENT
    wsgi_auth = get_wsgi_auth(auth, auth_type)
    if wsgi_auth:
        headers["Authorization"] = wsgi_auth
    return headers


def get_wsgi_auth(auth: Optional[RawAuth], auth_type: Optional[str]) -> Optional[str]:
    if auth:
        if auth_type == "digest":
            raise ValueError("Digest auth is not supported for WSGI apps")
        return _basic_auth_str(*auth)
    return None


def asgi_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    store_interactions: bool,
    headers: Optional[Dict[str, Any]],
    feedback: Feedback,
    max_response_time: Optional[int],
) -> None:
    """A single test body will be executed against the target."""
    headers = headers or {}

    response = _asgi_test(case, checks, targets, result, store_interactions, headers, feedback, max_response_time)
    add_cases(
        case, response, _asgi_test, checks, targets, result, store_interactions, headers, feedback, max_response_time
    )


def _asgi_test(
    case: Case,
    checks: Iterable[CheckFunction],
    targets: Iterable[Target],
    result: TestResult,
    store_interactions: bool,
    headers: Optional[Dict[str, Any]],
    feedback: Feedback,
    max_response_time: Optional[int],
) -> requests.Response:
    response = case.call_asgi(headers=headers)
    context = TargetContext(case=case, response=response, response_time=response.elapsed.total_seconds())
    run_targets(targets, context)
    status = Status.success
    check_results: List[Check] = []
    try:
        run_checks(case, checks, check_results, result, response, context.response_time * 1000, max_response_time)
    except CheckFailed:
        status = Status.failure
        raise
    finally:
        if store_interactions:
            result.store_requests_response(response, status, check_results)
    feedback.add_test_case(case, response)
    return response
