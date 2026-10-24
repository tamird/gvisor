"""Retain image owners, execution transitions and foreign build diagnostics."""

def format(target):
    """Return the exact configured owner and its declared output paths.

    Args:
      target: The configured target supplied by cquery.

    Returns:
      A JSON row containing owner, execution configuration and output groups.
    """
    groups = (providers(target) or {}).get("OutputGroupInfo")
    options = build_options(target) or {}
    names = [
        "Make_logs",
        "Configure_logs",
        "busybox.config",
        "apr.h",
        "apr_private.h",
        "apu_config.h",
        "ap_config_auto.h",
        "ap_config_layout.h",
        "config.log",
    ]
    return json.encode({
        "label": str(target.label),
        "name": target.label.name,
        "cpu": options.get("//command_line_option:cpu"),
        "is_exec": options.get("//command_line_option:is exec configuration"),
        "files": [file.path for file in target.files.to_list()],
        "groups": {
            name: [file.path for file in getattr(groups, name).to_list()]
            for name in names
            if hasattr(groups, name)
        },
    })
