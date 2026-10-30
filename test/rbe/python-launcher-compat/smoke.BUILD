load("@rules_python//python:py_binary.bzl", "py_binary")
py_binary(name="probe",srcs=["probe.py"],data=["payload.txt"], main="probe.py")
py_binary(name="multi",srcs=["probe.py"], data=["payload.txt"], main="probe.py", interpreter_args=["-XA=1","-XB=2"])
