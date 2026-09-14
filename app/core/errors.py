"""Errors shared across the download core."""


class DownloadError(Exception):
    """A download failed in a way the engine understands (not a bug).

    The message is user-facing: it ends up in the jobs table and the queue UI,
    so it must be a plain sentence, never a traceback.
    """
