class GatewayError(RuntimeError):
    """A tool, model or service this app runs on is missing or unusable. These
    are setup problems the user can fix, which is what separates them from bugs:
    they earn a single line of advice, never a traceback."""
