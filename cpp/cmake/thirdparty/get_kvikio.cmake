# =============================================================================
# Copyright (c) 2022-2023, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations under
# the License.
# =============================================================================

# This function finds KvikIO
function(find_and_configure_kvikio VERSION)
  # TODO 注意kvikio内部还有github CPM依赖 CURL
  # 位置如下 kvikio/ cpp / cmake / thirdparty / get_libcurl.cmake
  rapids_cpm_find(
    KvikIO ${VERSION}
    GLOBAL_TARGETS kvikio::kvikio
    CPM_ARGS
    GIT_REPOSITORY https://gitee.com/caork/kvikio.git
    GIT_TAG branch-${VERSION}
    GIT_SHALLOW TRUE SOURCE_SUBDIR cpp
    OPTIONS "KvikIO_BUILD_EXAMPLES OFF"
  )

  include("${rapids-cmake-dir}/export/find_package_root.cmake")
  rapids_export_find_package_root(
    BUILD KvikIO "${KvikIO_BINARY_DIR}"
    EXPORT_SET cudf-exports
    CONDITION KvikIO_BINARY_DIR
  )

endfunction()

set(KVIKIO_MIN_VERSION_cudf "${CUDF_VERSION_MAJOR}.${CUDF_VERSION_MINOR}")
find_and_configure_kvikio(${KVIKIO_MIN_VERSION_cudf})
