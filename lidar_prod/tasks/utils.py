import logging
import math
import subprocess
from dataclasses import dataclass
from numbers import Number
from typing import Any, Dict, Iterable

import geopandas
import laspy
import numpy as np
import pdal
import psycopg2

log = logging.getLogger(__name__)


@dataclass
class BDUniConnectionParams:
    """URL and public credentials to connect to a database - typically the BDUni"""

    host: str
    user: str
    pwd: str
    bd_name: str


def split_idx_by_dim(dim_array):
    """
    Returns a sequence of arrays of indices of elements sharing the same value in dim_array
    Groups are ordered by ascending value.
    """
    idx = np.argsort(dim_array)
    sorted_dim_array = dim_array[idx]
    group_idx = np.array_split(idx, np.where(np.diff(sorted_dim_array) != 0)[0] + 1)
    return group_idx


def get_pipeline(input_value: pdal.pipeline.Pipeline | str, epsg: int | str):
    """If the input value is a pipeline, returns it, if it's a las path return the corresponding
    pipeline

    Args:
        input_value (pdal.pipeline.Pipeline | str): input value to get a pipeline from
        (las pipeline or path to a file to read with pdal)
        epsg (int | str): if input_value is a string, use the epsg value to override the crs from
        the las header

    Returns:
        pdal pipeline
    """
    if isinstance(input_value, str):
        pipeline = pdal.Pipeline() | get_pdal_reader(input_value, epsg)
        pipeline.execute()
    else:
        pipeline = input_value
    return pipeline


def get_input_las_metadata(pipeline: pdal.pipeline.Pipeline):
    """Get las reader metadata from the input pipeline"""
    return pipeline.metadata["metadata"]["readers.las"]


def get_integer_bbox(pipeline: pdal.pipeline.Pipeline, buffer: Number = 0) -> Dict[str, int]:
    """Get XY bounding box of the las input of a pipeline, cast x/y min/max to integers.

    Args:
        pipeline (pdal.pipeline.Pipeline): pipeline for which to read the input bounding box
        buffer (Number, optional): buffer to add to the bounds before casting it to integers.
        Defaults to 0.

    Returns:
        Dict[str, int]: x/y min/max values as a dictionary
    """
    metadata = get_input_las_metadata(pipeline)
    bbox = {
        "x_min": math.floor(metadata["minx"] - buffer),
        "y_min": math.floor(metadata["miny"] - buffer),
        "x_max": math.ceil(metadata["maxx"] + buffer),
        "y_max": math.ceil(metadata["maxy"] + buffer),
    }
    return bbox


def get_pdal_reader(las_path: str, epsg: int | str) -> pdal.Reader.las:
    """Standard Reader which imposes Lamber 93 SRS.

    Args:
        las_path (str): input LAS path to read.
        epsg (int | str): epsg code for the input file (if empty or None: infer
        it from the las metadata)

    Returns:
        pdal.Reader.las: reader to use in a pipeline.

    """
    if epsg:
        reader = pdal.Reader.las(
            filename=las_path,
            nosrs=True,
            override_srs=(f"EPSG:{epsg}" if (isinstance(epsg, int) or epsg.isdigit()) else epsg),
        )
    else:
        reader = pdal.Reader.las(
            filename=las_path,
        )

    return reader


def get_las_data_from_las(las_path: str) -> laspy.lasdata.LasData:
    """Load las data from a las file"""
    return laspy.read(las_path)


def get_pdal_writer(target_las_path: str, extra_dims: str = "all") -> pdal.Writer.las:
    """Standard LAS Writer which imposes LAS 1.4 specification and dataformat 8.

    Args:
        target_las_path (str): output LAS path to write.
        extra_dims (str): extra dimensions to keep, in the format expected by pdal.Writer.las.

    Returns:
        pdal.Writer.las: writer to use in a pipeline.

    """
    return pdal.Writer.las(
        filename=target_las_path,
        minor_version=4,
        dataformat_id=8,
        forward="all",
        extra_dims=extra_dims,
    )


def save_las_data_to_las(las_path: str, las_data: laspy.lasdata.LasData):
    """save las data to a las file"""
    las_data.write(las_path)


def get_a_las_to_las_pdal_pipeline(
    src_las_path: str, target_las_path: str, ops: Iterable[Any], epsg: int | str
):
    """Create a pdal pipeline, preserving format, forwarding every dimension.

    Args:
        src_las_path (str): input LAS path
        target_las_path (str): output LAS path
        ops (Iterable[Any]): list of pdal operation (e.g. Filter.assign(...))
        epsg (int | str): epsg code for the input file (if empty or None: infer it from the
        las metadata)

    """
    pipeline = pdal.Pipeline()
    pipeline |= get_pdal_reader(src_las_path, epsg)
    for op in ops:
        pipeline |= op
    pipeline |= get_pdal_writer(target_las_path)
    return pipeline


def pdal_read_las_array(las_path: str, epsg: int | str):
    """Read LAS as a named array.

    Args:
        las_path (str): input LAS path
        epsg (int | str): epsg code for the input file (if empty or None: infer it from the
        las metadata)

    Returns:
        np.ndarray: named array with all LAS dimensions, including extra ones, with dict-like
        access.
    """
    p1 = pdal.Pipeline() | get_pdal_reader(las_path, epsg)
    p1.execute()
    return p1.arrays[0]


def check_bbox_intersects_territoire_with_srid(
    bd_params: BDUniConnectionParams, bbox: Dict[str, int], epsg_srid: int | str
):
    """Check if a bounding box intersects one of the territories from the BDUni database
    (public.gcms_territoire) with the expected srid.
    As geometries are indicated with srid = 0 in the database (but stored in their original
    projection),
    both geometries are compared using this common srid.
    In the territoire geometry query, ST_Union is used to combine different territoires that
    would have the same
    srid (eg. 5490 for Guadeloupe and Martinique)
    """
    conn = psycopg2.connect(
        dbname=bd_params.bd_name,
        user=bd_params.user,
        password=bd_params.pwd,
        host=bd_params.host,
    )
    try:
        with conn:
            with conn.cursor() as curs:
                query = f"""select ST_Intersects(
                    ST_MakeEnvelope(
                        {bbox["x_min"]}, {bbox["y_min"]}, {bbox["x_max"]}, {bbox["y_max"]}, 0
                        ),
                    ST_SetSRID(ST_Envelope(ST_Union(ST_Force2D(geometrie))),0))::bool
                    as consistency_bbox_srid
                    from public.gcms_territoire
                    where srid = '{epsg_srid}'
                    limit 1;
                """
                curs.execute(query)
                out = curs.fetchone()

    # Unlike file objects or other resources, exiting the connection’s with block doesn’t
    # close the connection hence we need to close it manually
    # cf https://www.psycopg.org/docs/usage.html#with-statement
    finally:
        conn.close()

    return out[0]


def request_bd_uni_for_building_shapefile(
    bd_params: BDUniConnectionParams,
    shapefile_path: str,
    bbox: Dict[str, int],
    epsg: int | str,
):
    """Request BD Uni for its buildings.

    Create a shapefile with non destructed building on the area of interest and saves it.

    Also add a "PRESENCE" column filled with 1 for later use by pdal.

    Note on the projections:
    Projections are mixed in the BDUni tables.
    In PostGIS, the declared projection is 0 but the data are stored in the legal projection of
    the corresponding territories.
    In each table, there is a a "gcms_territoire" field, which tells the corresponding territory
    (3 letters code).
    The gcms_territoire table gives hints on each territory (SRID, footprint)
    """

    epsg_srid = epsg if (isinstance(epsg, int) or epsg.isdigit()) else epsg.split(":")[-1]

    if not check_bbox_intersects_territoire_with_srid(bd_params, bbox, epsg_srid):
        raise ValueError(
            f"The query bbox ({bbox}) does not intersect with any territoire in the database with "
            + f"the query srid ({epsg_srid}). Please check that you passed the correct srid."
        )

    sql_territoire = f"""WITH territoire(code) as (SELECT code FROM public.gcms_territoire \
        WHERE srid = {epsg_srid}) """

    sql_batiment = f"""SELECT \
        ST_MakeValid(ST_Force2D(st_setsrid(batiment.geometrie,{epsg_srid}))) AS geometry, \
        1 as presence \
        FROM batiment, territoire \
        WHERE (batiment.gcms_territoire = territoire.code) \
        AND batiment.geometrie \
        && ST_MakeEnvelope({bbox["x_min"]}, {bbox["y_min"]}, {bbox["x_max"]}, {bbox["y_max"]}, 0) \
        AND not gcms_detruit"""

    sql_reservoir = f"""SELECT \
        ST_MakeValid(ST_Force2D(st_setsrid(reservoir.geometrie,{epsg_srid}))) AS geometry, \
        1 as presence \
        FROM reservoir, territoire \
        WHERE (reservoir.gcms_territoire = territoire.code) \
        AND reservoir.geometrie \
        && ST_MakeEnvelope({bbox["x_min"]}, {bbox["y_min"]}, {bbox["x_max"]}, {bbox["y_max"]}, 0) \
        AND (reservoir.nature = 'Château d''eau' OR reservoir.nature = 'Réservoir industriel') \
        AND NOT gcms_detruit"""

    sql_select_list = [sql_batiment, sql_reservoir]
    sql_request = sql_territoire + " UNION ".join(sql_select_list)

    cmd = f"""pgsql2shp -f {shapefile_path} \
                        -h {bd_params.host} \
                        -u {bd_params.user} \
                        -P {bd_params.pwd} \
                        {bd_params.bd_name} \
                        \"{sql_request}\""""

    # This call may yield
    try:
        subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=120, encoding="utf-8"
        )
    except subprocess.CalledProcessError as e:
        # In empty zones, pgsql2shp does not create a shapefile
        if (
            b"Initializing... \nERROR: Could not determine table metadata (empty table)\n"
            in e.output
        ):
            # write empty shapefile
            df = geopandas.GeoDataFrame(
                columns=["id", "geometry"],
                geometry="geometry",
                crs=f"EPSG:{epsg_srid}",
            )
            df.to_file(shapefile_path)

            return False
        # Error can be due to something else entirely, like
        # an inability to translate host name to an address.
        # e.g. "could not translate host name "serveurbdudiff.ign.fr" to address: System error"
        raise e
    except ConnectionRefusedError as e:
        log.error(
            "ConnectionRefusedError when requesting BDUni.  \
            This means that the Database cannot be accessed (e.g. due to vpn/proxy reasons, \
            or bad credentials)"
        )
        raise e
    except subprocess.TimeoutExpired as e:
        log.error("Time out when requesting BDUni.")
        raise e

    return True
