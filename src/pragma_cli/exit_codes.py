"""Exit codes every ``pragma`` command shares, and how an API error maps to one."""

FAILURE_EXIT_CODE = 1
"""Exit code for a failure: the API refused or could not complete the request, or the command could not finish."""

INPUT_ERROR_EXIT_CODE = 2
"""Exit code for invalid usage or a local file or config error, matching Typer's own usage errors."""

ADMISSION_UNCONFIRMED_EXIT_CODE = 3
"""Exit code of ``pragma providers publish`` once the API accepted the upload but admission is not confirmed.

The watch ended with the version still admitting, or failed for any reason
after the upload was accepted.
"""

NOT_FOUND_EXIT_CODE = 4
"""Exit code when the API answers 404.

The target does not exist, is not installed, has no deployment, or is not
visible to the caller.
"""

EXIT_CODES_HELP = (
    f"Exit codes: 0 success; {FAILURE_EXIT_CODE} failure; "
    f"{INPUT_ERROR_EXIT_CODE} invalid usage or a local file or config error; "
    f"{ADMISSION_UNCONFIRMED_EXIT_CODE} command-specific, named in that command's help "
    "(providers publish: upload accepted, admission not confirmed); "
    f"{NOT_FOUND_EXIT_CODE} not found: the target does not exist, is not installed, has no deployment, "
    "or is not visible to you. "
    "Errors and warnings go to stderr; command output, including -o json, goes to stdout."
)
"""Exit-code and output-stream contract shown in ``pragma --help``."""


def compute_http_exit_code(status_code: int) -> int:
    """Compute the exit code for an API error response.

    Args:
        status_code: HTTP status code of the API's error response.

    Returns:
        ``NOT_FOUND_EXIT_CODE`` for 404, else ``FAILURE_EXIT_CODE``.
    """
    if status_code == 404:
        return NOT_FOUND_EXIT_CODE

    return FAILURE_EXIT_CODE
