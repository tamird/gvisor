load("@rules_python//python:defs.bzl","py_binary","py_library")
py_library(name="inner_lib",srcs=["inner_lib.py"])
py_binary(name="outer",srcs=["outer.py"],main="outer.py")
py_binary(name="inner",srcs=["inner.py"],main="inner.py",deps=[":inner_lib"])
