#=============================================================================
# Copyright (c) 2021-2024, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#=============================================================================
#
# This is the preferred entry point for projects using rapids-cmake
#

# Allow users to control which version is used
if(NOT rapids-cmake-version)
    # Define a default version if the user doesn't set one
    set(rapids-cmake-version 24.12)
endif()

# Allow users to control which GitHub repo is fetched
if(NOT rapids-cmake-repo)
    # Define a default repo if the user doesn't set one
    set(rapids-cmake-repo rapidsai/rapids-cmake)
endif()

# Allow users to control which branch is fetched
if(NOT rapids-cmake-branch)
    # Define a default branch if the user doesn't set one
    set(rapids-cmake-branch "branch-${rapids-cmake-version}")
endif()
# TODO 直接定义CMAKE包位置
execute_process (
        COMMAND bash -c "zip -r ${CMAKE_SOURCE_DIR}/offline_data/rapids_make/rapids-cmake-branch-24.12.zip ${CMAKE_SOURCE_DIR}/offline_data/rapids_make/rapids-cmake-branch-24.12"
        OUTPUT_VARIABLE ZIP_STATUS
)
message("zip rapids cmake: ${ZIP_STATUS}")
set(rapids-cmake-url "${CMAKE_SOURCE_DIR}/offline_data/rapids_make/rapids-cmake-branch-24.12.zip")
# Allow users to control the exact URL passed to FetchContent
if(NOT rapids-cmake-url)
    # Construct a default URL if the user doesn't set one
    message("rapids-cmake-repo: ${rapids-cmake-repo}")
    set(rapids-cmake-url "https://github.com/${rapids-cmake-repo}/")

    # In order of specificity
    message("rapids-cmake-fetch-via-git: ${rapids-cmake-fetch-via-git}")
    if(rapids-cmake-fetch-via-git)
        if(rapids-cmake-sha)
            # An exact git SHA takes precedence over anything
            set(rapids-cmake-value-to-clone "${rapids-cmake-sha}")
        elseif(rapids-cmake-tag)
            # Followed by a git tag name
            set(rapids-cmake-value-to-clone "${rapids-cmake-tag}")
        else()
            # Or if neither of the above two were defined, use a branch
            set(rapids-cmake-value-to-clone "${rapids-cmake-branch}")
        endif()
    else()
        if(rapids-cmake-sha)
            # An exact git SHA takes precedence over anything
            set(rapids-cmake-value-to-clone "archive/${rapids-cmake-sha}.zip")
        elseif(rapids-cmake-tag)
            # Followed by a git tag name
            set(rapids-cmake-value-to-clone "archive/refs/tags/${rapids-cmake-tag}.zip")
        else()
            # Or if neither of the above two were defined, use a branch
            set(rapids-cmake-value-to-clone "archive/refs/heads/${rapids-cmake-branch}.zip")
        endif()
    endif()
endif()
message("rapids-cmake-value-to-clone: ${rapids-cmake-value-to-clone}")
if(POLICY CMP0135)
    cmake_policy(PUSH)
    cmake_policy(SET CMP0135 NEW)
endif()
include(FetchContent)
# TODO 直接选择本地包跳过在线下载
set(LOCAL_RAPIDS_CMAKE "TRUE")
if(LOCAL_RAPIDS_CMAKE)
    message("use local rapids cmake zip file")
    message("rapids-cmake-url: ${rapids-cmake-url}")
    FetchContent_Declare(rapids-cmake
            URL "${rapids-cmake-url}")
elseif(rapids-cmake-fetch-via-git)
    message("rapids-cmake-url: ${rapids-cmake-url}")
    FetchContent_Declare(rapids-cmake
            GIT_REPOSITORY "${rapids-cmake-url}"
            GIT_TAG "${rapids-cmake-value-to-clone}")
else()
    string(APPEND rapids-cmake-url "${rapids-cmake-value-to-clone}")
    message("appended rapids-cmake-url: ${rapids-cmake-url}")
    FetchContent_Declare(rapids-cmake URL "${rapids-cmake-url}")
endif()
if(POLICY CMP0135)
    cmake_policy(POP)
endif()
message("rapids_make: ${rapids-cmake}")
FetchContent_GetProperties(rapids-cmake)
if(rapids-cmake_POPULATED)
    # Something else has already populated rapids-cmake, only thing
    # we need to do is setup the CMAKE_MODULE_PATH
    if(NOT "${rapids-cmake-dir}" IN_LIST CMAKE_MODULE_PATH)
        list(APPEND CMAKE_MODULE_PATH "${rapids-cmake-dir}")
    endif()
else()
    FetchContent_MakeAvailable(rapids-cmake)
endif()
