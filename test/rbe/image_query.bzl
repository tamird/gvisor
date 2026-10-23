"""Retain configured owners and existing foreign-build output groups."""

def format(target):
    """Emit the configured target and its declared diagnostic groups.

    Args:
      target: The configured target supplied by cquery.

    Returns:
      A JSON row containing the owner, CPU, files and diagnostic groups.
    """
    groups = (providers(target) or {}).get("OutputGroupInfo")
    names = ["Make_logs", "Configure_logs", "busybox.config", "ngx_auto_config.h"]
    return json.encode({
        "label": str(target.label),
        "name": target.label.name,
        "cpu": (build_options(target) or {}).get("//command_line_option:cpu"),
        "files": [file.path for file in target.files.to_list()],
        "groups": {
            name: [file.path for file in getattr(groups, name).to_list()]
            for name in names
            if hasattr(groups, name)
        },
    })
