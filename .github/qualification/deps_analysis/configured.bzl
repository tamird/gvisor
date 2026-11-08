def format(target):
    advertised = providers(target)
    groups = advertised.get("OutputGroupInfo")
    validation = getattr(groups, "_validation", None)
    analysis = advertised.get("AnalysisTestResultInfo")
    return json.encode({
        "label": str(target.label),
        "platform": str(build_options(target)["//command_line_option:platforms"]),
        "default_files": [f.path for f in target.files.to_list()],
        "validation": sorted([f.path for f in validation.to_list()]) if validation else [],
        "analysis_result": [analysis.success, analysis.message] if analysis else None,
    })
