"""Shared names for the agent code-execution sandbox setting.

``security_layer.code_execution`` is validated in ``runtime_settings.security``
and enforced in ``custom_tools.code_sandbox``. The launcher passes the
resolved values to runtime processes through the environment variables below.
Keeping the names here lets configuration, launch, and enforcement agree
without the settings layer importing tool code.
"""

CODE_SANDBOX_REQUIRED = "required"
CODE_SANDBOX_OFF = "off"
CODE_SANDBOX_MODES = (CODE_SANDBOX_REQUIRED, CODE_SANDBOX_OFF)

ENV_CODE_SANDBOX_MODE = "CODE_SANDBOX_MODE"
ENV_CODE_SANDBOX_EXTRA_READ_PATHS = "CODE_SANDBOX_EXTRA_READ_PATHS"
ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS = "CODE_SANDBOX_EXTRA_WRITE_PATHS"
