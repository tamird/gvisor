def format(target):
    opts = build_options(target) or {}
    label = str(target.label)
    if label.startswith("@@//") or label.startswith("@//"):
        label = label.lstrip("@")
    cpu = str(opts.get("//command_line_option:cpu", ""))
    platforms = [str(p) for p in opts.get("//command_line_option:platforms", [])]
    files = [[f.short_path, f.is_source, f.is_directory, f.path, str(f.owner)] for f in target.files.to_list()]
    return json.encode([label, cpu, platforms, opts.get("//command_line_option:is exec configuration", False), files])
