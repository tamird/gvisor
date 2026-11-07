def _impl(_ctx):
    return []

node = rule(implementation = _impl, attrs = {"deps": attr.label_list()})
