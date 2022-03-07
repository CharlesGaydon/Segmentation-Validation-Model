FROM nvidia/cuda:11.3.1-devel-ubuntu20.04

# set the IGN proxy, otherwise apt-get and other applications don't work 
ENV http_proxy 'http://192.168.4.9:3128/'
ENV https_proxy 'http://192.168.4.9:3128/'

# set the timezone, otherwise it asks for it... and freezes
ENV TZ=Europe/Paris
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# all the apt-get installs
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
        software-properties-common  \
        wget                        \
        git                         \
        postgis                     \
        pdal                        \
        libgl1-mesa-glx libegl1-mesa libxrandr2 libxrandr2 libxss1 libxcursor1 libxcomposite1 libasound2 libxi6 libxtst6   # package needed for anaconda

# install anaconda
RUN wget --quiet https://repo.anaconda.com/archive/Anaconda3-2021.11-Linux-x86_64.sh -O ~/anaconda.sh
RUN /bin/bash ~/anaconda.sh -b -p /opt/conda && \
    rm ~/anaconda.sh
ENV PATH /opt/conda/bin:$PATH

# copy all the data now (because the requirements files are needed for anaconda)
COPY . .

# update the requirements.txt path indicated in requirements.yml, to be able to use it in docker
RUN sed -i "s|requirements.txt|/bash/setup_environment/requirements.txt |g" /bash/setup_environment/requirements.yml
# install the python packages via anaconda
RUN conda env create -f /bash/setup_environment/requirements.yml

# Make RUN commands use the new environment:
SHELL ["conda", "run", "-n", "lidar_prod", "/bin/bash", "-c"]

# test if pdal is installed (a tricky library!)
RUN echo "Make sure pdal is installed:"
RUN python -c "import pdal"

# the command executed by this docker image
ENTRYPOINT ["conda",                \   
            "run",                  \
            "-n",                   \
            "lidar_prod",           \
            "python",               \
            "/lidar_prod/run.py",   \
            "print_config=true",    \
            "+task='optimize'",     \
            "+building_validation.optimization.debug=true", \
            "building_validation.optimization.todo='prepare+evaluate+update'", \
            "building_validation.optimization.paths.input_las_dir=/CICD_github_assets/M8.0/20220204_building_val_V0.0_model/20211001_buiding_val_val/", \
            "building_validation.optimization.paths.results_output_dir=/CICD_github_assets/opti/", \
            "building_validation.optimization.paths.building_validation_thresholds_pickle=/CICD_github_assets/M8.0/20220204_building_val_V0.0_model/M8.0B2V0.0_buildingvalidation_thresholds.pickle"]
