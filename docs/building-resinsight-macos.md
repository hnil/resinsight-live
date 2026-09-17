# Building ResInsight with its Python interface on macOS

Only needed when not using an official release. Configure a separate build directory:

    cmake -S . -B builds-grpc -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DRESINSIGHT_ENABLE_GRPC=ON -DCMAKE_PREFIX_PATH=/opt/homebrew \
      -DCMAKE_C_COMPILER=/usr/bin/cc -DCMAKE_CXX_COMPILER=/usr/bin/c++ \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_TESTING=OFF -DBUILD_TESTS=OFF \
      -DRESINSIGHT_GRPC_PYTHON_EXECUTABLE=/path/to/a/venv/bin/python
    cmake --build builds-grpc

With Homebrew's gRPC and protobuf:

- `RESINSIGHT_GRPC_PYTHON_EXECUTABLE` must be a virtual environment with `grpcio-tools`, not
  Homebrew's Python: the `PipInstall` target is part of `all` and fails on an
  externally-managed interpreter.
- Newer protobuf (seen with 34.1) returns `absl::string_view` from descriptor `name()`, which
  breaks `GrpcInterface/RiaGrpcCommandService.cpp` in three places. The fix is a
  `std::string(...)` conversion around each call; this is not in upstream ResInsight yet.
- `CMAKE_POLICY_VERSION_MINIMUM=3.5` is needed for the bundled googletest with CMake 4.

The build generates the Python client (`GrpcInterface/Python/rips/generated/`) by running
ResInsight itself, so `rips` can be installed from the source tree with
`pip install -e GrpcInterface/Python` instead of from PyPI.
