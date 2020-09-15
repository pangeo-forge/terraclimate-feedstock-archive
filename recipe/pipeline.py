import os
from datetime import timedelta
from typing import List

import dask
import fsspec
import xarray as xr
from numcodecs import Blosc
from pangeo_forge.pipelines.base import AbstractPipeline
from pangeo_forge.tasks.http import download
from prefect import Flow, task, unmapped
from prefect.environments import DaskKubernetesEnvironment
from prefect.environments.storage import Docker

# options
name = "terraclimate"
chunks = {"lat": 1024, "lon": 1024, "time": 12}
# years = list(range(1958, 2020))
years = list(range(1958, 1960))
cache_location = f"gs://pangeo-forge-scratch/{name}-cache-2/"
target_location = f"gs://pangeo-forge-scratch/{name}.zarr"


variables = [
    "aet",
    "def",
    "pet",
    "ppt",
    "q",
    "soil",
    "srad",
    "swe",
    "tmax",
    "tmin",
    "vap",
    "ws",
    "vpd",
    "PDSI",
]

rename_vars = {'PDSI': 'pdsi'}

mask_opts = {
    "PDSI": ("lt", 10),
    "aet": ("lt", 32767),
    "def": ("lt", 32767),
    "pet": ("lt", 32767),
    "ppt": ("lt", 32767),
    "ppt_station_influence": None,
    "q": ("lt", 2147483647),
    "soil": ("lt", 32767),
    "srad": ("lt", 32767),
    "swe": ("lt", 10000),
    "tmax": ("lt", 200),
    "tmax_station_influence": None,
    "tmin": ("lt", 200),
    "tmin_station_influence": None,
    "vap": ("lt", 300),
    "vap_station_influence": None,
    "vpd": ("lt", 300),
    "ws": ("lt", 200),
}


def apply_mask(key, da):
    """helper function to mask DataArrays based on a threshold value"""
    if mask_opts.get(key, None):
        op, val = mask_opts[key]
        if op == "lt":
            da = da.where(da < val)
        elif op == "neq":
            da = da.where(da != val)
    return da


def preproc(ds):
    """custom preprocessing function for terraclimate data"""
    rename = {}

    station_influence = ds.get("station_influence", None)

    if station_influence is not None:
        ds = ds.drop_vars("station_influence")

    var = list(ds.data_vars)[0]

    if var in rename_vars:
        rename[var] = rename_vars[var]

    if "day" in ds.coords:
        rename["day"] = "time"

    if station_influence is not None:
        ds[f"{var}_station_influence"] = station_influence

    if rename:
        ds = ds.rename(rename)

    return ds


def postproc(ds):
    """custom post processing function to clean up terraclimate data"""
    drop_encoding = [
        "chunksizes",
        "fletcher32",
        "shuffle",
        "zlib",
        "complevel",
        "dtype",
        "_Unsigned",
        "missing_value",
        "_FillValue",
        "scale_factor",
        "add_offset",
    ]
    for v in ds.data_vars.keys():
        with xr.set_options(keep_attrs=True):
            ds[v] = apply_mask(v, ds[v])
        for k in drop_encoding:
            ds[v].encoding.pop(k, None)

    return ds


def get_encoding(ds):
    compressor = Blosc()
    encoding = {key: {"compressor": compressor} for key in ds.data_vars}
    return encoding


@task(max_retries=1, retry_delay=timedelta(seconds=1))
def download(source_url: str, cache_location: str) -> str:
    """
    Download a remote file to a cache.
    Parameters
    ----------
    source_url : str
        Path or url to the source file.
    cache_location : str
        Path or url to the target location for the source file.
    Returns
    -------
    target_url : str
        Path or url in the form of `{cache_location}/hash({source_url})`.
    """
    fs = fsspec.get_filesystem_class(cache_location.split(':')[0])(token='cloud')

    target_url = os.path.join(cache_location, str(hash(source_url)))

    # there is probably a better way to do caching!
    try:
        fs.open(target_url)
        return target_url
    except FileNotFoundError:
        pass

    with fsspec.open(source_url, mode="rb") as source:
        with fs.open(target_url, mode="wb") as target:
            target.write(source.read())
    return target_url


@task(max_retries=1, retry_delay=timedelta(seconds=10))
def nc2zarr(source_url: str, cache_location: str) -> str:
    """convert netcdf data to zarr"""
    fs = fsspec.get_filesystem_class(source_url.split(':')[0])(token='cloud')

    target_url = source_url + ".zarr"

    with dask.config.set(scheduler="single-threaded"):

        ds = (
            xr.open_dataset(fs.open(source_url))
            .pipe(preproc)
            .pipe(postproc)
            .load()
            .chunk(chunks)
        )

        mapper = fs.get_mapper(target_url)
        ds.to_zarr(mapper, mode='w')

    return target_url


@task
def combine_and_write(sources: List[str], target: str):
    """
    Combine one or more source datasets into a single `Xarray.Dataset`, then
    write them to a Zarr store.
    Parameters
    ----------
    sources : list of str
        Path or url to the source files.
    target : str
        Path or url to the target location of the Zarr store.
    """
    mappers = [fsspec.get_mapper(url) for url in sources]
    ds_list = [xr.open_zarr(mapper) for mapper in mappers]

    ds = xr.combine_by_coords(ds_list, combine_attrs='override')

    mapper = fsspec.get_mapper(target)
    ds.to_zarr(mapper, mode="w", consolidated=True)


class TerraclimatePipeline(AbstractPipeline):
    def __init__(self, cache_location, target_location, variables, years):
        self.name = name
        self.cache_location = cache_location
        self.target_location = target_location
        self.variables = variables
        self.years = years

    @property
    def sources(self):

        source_url_pattern = "https://climate.northwestknowledge.net/TERRACLIMATE-DATA/TerraClimate_{var}_{year}.nc"
        source_urls = []

        for var in self.variables:
            for year in self.years:
                source_urls.append(source_url_pattern.format(var=var, year=year))

        return source_urls

    @property
    def targets(self):
        return [self.target_location]

    @property
    def environment(self):
        environment = DaskKubernetesEnvironment(
            min_workers=1, max_workers=30,
            scheduler_spec_file="recipe/job.yaml",
            worker_spec_file="recipe/worker_pod.yaml",
        )
        return environment

    @property
    def storage(self):
        storage = Docker(
            "pangeoforge",
            dockerfile="recipe/Dockerfile",
            prefect_directory="/home/jovyan/prefect",
            python_dependencies=[
                "git+https://github.com/pangeo-forge/pangeo-forge@master",
                "prefect==0.13.6",
            ],
            image_tag="latest",
        )
        return storage

    @property
    def flow(self):

        if len(self.targets) == 1:
            target = self.targets[0]
        else:
            raise ValueError("Zarr target requires self.targets be a length one list")

        with Flow(self.name, storage=self.storage, environment=self.environment) as _flow:
            # download to cache
            nc_sources = download.map(
                self.sources,
                cache_location=unmapped(self.cache_location),
            )

            # convert cached netcdf data to zarr
            cached_sources = nc2zarr.map(
                nc_sources,
                cache_location=unmapped(self.cache_location),
            )

            # combine all datasets into a single zarr archive
            combine_and_write(cached_sources, target)

        return _flow


pipeline = TerraclimatePipeline(cache_location, target_location, variables, years)


if __name__ == "__main__":
    pipeline.flow.validate()

    print(pipeline.flow)
    print(pipeline.flow.environment)
    print(pipeline.flow.parameters)
    print(pipeline.flow.sorted_tasks())
    print("Registering Flow")
    pipeline.flow.register(project_name="pangeo-forge")

