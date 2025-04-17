# This dockerfile is based on the OCS2 dockerfile from "https://github.com/minghongx/ocs2.git"

FROM ubuntu:focal
LABEL Name=ocs2 Version=2022.08.05

# https://stackoverflow.com/questions/28405902/how-to-set-the-locale-inside-a-debian-ubuntu-docker-container
# Workaround: https://hub.docker.com/_/debian/
RUN apt-get update && apt-get install --yes locales && rm -rf /var/lib/apt/lists/* \
    && localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG en_US.utf8

# Workaround: https://askubuntu.com/questions/432604/couldnt-connect-to-accessibility-bus
ENV NO_AT_BRIDGE=1

RUN apt-get update && apt-get install --yes --no-install-recommends \
        gnupg \
        wget \
    && wget http://packages.ros.org/ros.key -O - | apt-key add - && \
       echo "deb http://packages.ros.org/ros/ubuntu focal main" > /etc/apt/sources.list.d/ros-latest.list \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        # Both GCC 11 and Clang fail to compile OCS2. It seems that only GCC 9 can be used
        build-essential \
        make \
        cmake \
        ros-noetic-cmake-modules \
        # Catkin
        ros-noetic-pybind11-catkin python3-catkin-tools \
        # Eigen 3.3, GLPK, and Boost C++ 1.71
        libeigen3-dev libglpk-dev libboost-all-dev \
        # Doxyge
        doxygen doxygen-latex graphviz \
        # Dependencies of hpp-fcl and pinocchio
        liburdfdom-dev liboctomap-dev libassimp-dev \
        # Dependencies of blasfeo_catkin
        git \
        # Make the installation of raisimLib easy to find for catkin and easy to uninstall in the future
        checkinstall \
        # Official examples launch several terminal emulators to form a dashboard
        gnome-terminal \
        dbus-x11 \
        libcanberra-gtk-module libcanberra-gtk3-module \
        # Misc
        ros-noetic-common-msgs \
        ros-noetic-interactive-markers \
        ros-noetic-tf \
        ros-noetic-urdf \
        ros-noetic-kdl-parser \
        ros-noetic-robot-state-publisher \
        ros-noetic-rviz \
        ros-noetic-grid-map-rviz-plugin \
        # Optional
        ros-noetic-rqt-multiplot \
        ros-noetic-grid-map-msgs \
        ros-noetic-eigenpy\
        ros-noetic-ur-description\
        python3-pip \
        ros-noetic-realtime-tools \ 
    # Prune
    && rm -rf /var/lib/apt/lists/*



WORKDIR /tmp/install
RUN git clone --recurse-submodules https://github.com/raisimTech/raisimLib.git raisimLib

# RaiSim is not supported on Linux ARM64. See https://github.com/raisimTech/raisimLib/issues/284
RUN cd raisimLib && mkdir build && cd build \
    && cmake .. -DRAISIM_EXAMPLE=ON -DRAISIM_PY=ON -DPYTHON_EXECUTABLE=$(python3 -c "import sys; print(sys.executable)") \
    && make -j4 && checkinstall \
    && rm -rf /tmp/* /var/tmp/*




# Installing OCS2
WORKDIR /catkin_ws/src

# Setup hpp-fcl
RUN git clone --recurse-submodules https://github.com/leggedrobotics/hpp-fcl.git hpp-fcl
RUN catkin init --workspace .. \
    && catkin config --extend /opt/ros/noetic \
    && catkin config -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_PYTHON_INTERFACE=ON -DCMAKE_INSTALL_PREFIX=/usr/local -DPYTHON_EXECUTABLE=$(python3 -c "import sys; print(sys.executable)") \
    && catkin build hpp-fcl 

# Setup pinocchio
RUN git clone --recurse-submodules https://github.com/leggedrobotics/pinocchio.git pinocchio
RUN catkin init --workspace .. \
    && catkin config --extend /opt/ros/noetic \
    && catkin config -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_PYTHON_INTERFACE=ON -DCMAKE_INSTALL_PREFIX=/usr/local -DPYTHON_EXECUTABLE=$(python3 -c "import sys; print(sys.executable)") -DBUILD_WITH_COLLISION_SUPPORT=ON\
    && catkin build pinocchio 

# Setup ocs2
RUN git clone --recurse-submodules https://github.com/leggedrobotics/ocs2_robotic_assets.git ocs2_robotic_assets
RUN git clone --branch upright --recurse-submodules https://github.com/utiasDSL/ocs2.git ocs2

COPY config.yaml /catkin_ws/.catkin_tools/profiles/default/
RUN catkin init --workspace .. \
    && catkin config --extend /opt/ros/noetic \
    && catkin config -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    && catkin build ocs2

RUN echo "[ -f /catkin_ws/devel/setup.bash ] && source /catkin_ws/devel/setup.bash" >> ~/.bashrc
# You can test the ocs2 installation here: roslaunch ocs2_ballbot_ros ballbot.launch


# Setup mobile_manipulation_central
RUN git clone --recurse-submodules https://github.com/utiasDSL/mobile_manipulation_central.git mobile_manipulation_central
WORKDIR /catkin_ws/src/mobile_manipulation_central
RUN python3 -m pip install -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /catkin_ws/src
RUN catkin init --workspace .. \
    && catkin config --extend /opt/ros/noetic \
    && catkin config -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    && catkin build mobile_manipulation_central


# Upgrade pip -- > need to install pinocchio (pin)
RUN python3 -m pip install --upgrade pip \                
    && rm -rf /var/lib/apt/lists/*

### Setup Upright
RUN git clone --recurse-submodules  https://github.com/utiasDSL/upright
WORKDIR /catkin_ws/src/upright
RUN python3 -m pip install -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /catkin_ws/src
RUN catkin init --workspace .. \
    && catkin config --extend /opt/ros/noetic \
    && catkin config -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    && catkin build
#
# Pinocchio install is broken
RUN python3 -m pip uninstall -y pin \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install pin \
    && rm -rf /var/lib/apt/lists/*

# Test the installation run the following command
# /catkin_ws/src/upright/upright_cmd/scripts/simulations/mpc_sim.py --config $(rospack find upright_cmd)/config/demos/thing_demo.yaml
# /catkin_ws/src/upright/upright_cmd/scripts/simulations/mpc_sim.py --config $(rospack find upright_cmd)/config/demos/ur5e_demo.yaml
# /catkin_ws/src/upright/upright_robust/scripts/planning_sim_loop.py --config $(rospack find upright_robust)/config/demos/simUR5e.yaml --height 30 --com robust --gui
