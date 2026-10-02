from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .core.binaries import find_ffmpeg, find_ffprobe, find_icon
from .core.logging_setup import configure_logging
from .gui.main_window import APP_NAME, MainWindow


def _self_check(allow_missing_ffmpeg: bool = False) -> int:
    """Verify a packaged build can start and find its bundled dependencies.

    Used by ``scripts/build_exe.ps1`` and safe to run on a normal machine.  It
    builds the real window, so a broken bundle fails here instead of showing
    the user an empty or crashing program.  The result is written to the log
    because a windowed executable has no console to print to.
    """

    logger = configure_logging().logger
    problems: list[str] = []
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        # A slim build deliberately ships no vendor folder and fetches FFmpeg on
        # first run, so an absent copy is its intended state rather than a
        # symptom.  It is still checked and still logged, so a slim build is
        # never quietly assumed to be fine.
        if allow_missing_ffmpeg:
            logger.info("self_check ffmpeg=missing (expected for a slim build)")
        else:
            problems.append("ffmpeg was not found")
    # ffprobe is optional. The application never calls it, and it is not
    # bundled, so a copy found on PATH is reported but never required.
    ffprobe = find_ffprobe()

    window = MainWindow()
    window.show()
    if not window.theme_combo.isEnabled():
        problems.append("the theme selector is disabled")

    icon = find_icon()
    logger.info(
        "self_check ffmpeg=%s ffprobe=%s icon=%s problems=%d",
        "found" if ffmpeg else "missing",
        "found" if ffprobe else "absent (optional)",
        "found" if icon else "absent",
        len(problems),
    )
    window.close()

    for problem in problems:
        logger.error("self_check_problem detail=%s", problem)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--check" in arguments or "--self-check" in arguments:
        # A headless-friendly platform keeps this usable in CI or a service
        # session where no display is attached.
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication([sys.argv[0], *arguments])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # Without an explicit icon the Windows shell falls back to the default
    # Python one, so the taskbar entry and the Alt+Tab entry both look wrong.
    # Set on the application so every window inherits it, and again on the
    # window itself because that is the one that ends up on the taskbar.
    icon: QIcon | None = None
    icon_path = find_icon()
    if icon_path is not None:
        candidate = QIcon(str(icon_path))
        # A .ico Qt cannot decode produces a null icon rather than an error, and
        # setting that would clear any icon the shell had already picked up from
        # the executable, so it is only set when something was really loaded.
        if not candidate.isNull():
            icon = candidate
            app.setWindowIcon(icon)
    logging_handle = configure_logging()
    logging_handle.logger.info("application_started")
    try:
        if "--check" in arguments or "--self-check" in arguments:
            return _self_check("--allow-missing-ffmpeg" in arguments)
        window = MainWindow()
        if icon is not None:
            # Set on the window as well: this frameless window is the one that
            # owns the taskbar button, and it does not always inherit cleanly.
            window.setWindowIcon(icon)
        # Ask before downloading missing external dependencies.  This never blocks
        # the launch, and a decline or a failure does not stop the program from
        # running.  The check is idempotent: after FFmpeg is installed, nothing
        # happens here again.
        try:
            from .gui.dependencies import first_run_check

            first_run_check(window)
        except Exception as error:
            logging_handle.logger.error(
                "dependency_prompt_failed exception=%s",
                type(error).__name__,
            )
        window.show()
        return app.exec()
    finally:
        logging_handle.logger.info("application_stopped")
        logging_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
