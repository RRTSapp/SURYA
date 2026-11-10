import streamlit as st
import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.collections import LineCollection
from shapely.geometry import Polygon
from io import BytesIO
from scipy.interpolate import griddata, Rbf
import simplekml
from pyproj import Transformer, CRS
from matplotlib.ticker import FormatStrFormatter

from modules.terrain.terrain_analyzer import run_terrain_analysis, run_survey_controlled_terrain_analysis
from modules.terrain.survey_parser import parse_survey
from modules.terrain.zone_classifier import build_clipped_terrain_cells
from modules.drainage.flow_analysis import run_d8_flow_screening, build_regular_dem_flow_grid
from modules.drainage.grading import design_finished_surface, calculate_weighted_suitability
from modules.drainage.surface_control import build_survey_controlled_surface


# -------------------------------------------------
# Helpers
# -------------------------------------------------

def get_utm_epsg_from_lonlat(lon: float, lat: float) -> int:
    zone = int((lon + 180) / 6) + 1
    return 32600 + zone if lat >= 0 else 32700 + zone


def format_geographic_axes(ax):
    """Show complete WGS84 coordinates; suppress Matplotlib offset notation."""
    ax.ticklabel_format(axis="both", style="plain", useOffset=False)
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.5f"))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.5f"))
    ax.set_xlabel("Longitude (°E)")
    ax.set_ylabel("Latitude (°N)")


def survey_coordinates_in_utm(spot_heights: pd.DataFrame, epsg: int):
    """Transform parser WGS84 lon/lat to project UTM; never alter source columns."""
    if spot_heights.empty:
        return spot_heights.copy(), 0.0
    source = spot_heights[["lon", "lat"]].astype(float).to_numpy()
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{int(epsg)}", always_xy=True)
    east, north = transformer.transform(source[:, 0], source[:, 1])
    out = spot_heights.copy()
    out["easting_m"] = east
    out["northing_m"] = north
    reverse = Transformer.from_crs(f"EPSG:{int(epsg)}", "EPSG:4326", always_xy=True)
    lon_back, lat_back = reverse.transform(east, north)
    residual = np.hypot((np.asarray(lon_back) - source[:, 0]) * 111320.0 * np.cos(np.deg2rad(source[:, 1])),
                        (np.asarray(lat_back) - source[:, 1]) * 110574.0)
    return out, float(np.nanmax(residual))


def generate_obstruction_kmz(df: pd.DataFrame, boundary: Polygon) -> BytesIO:
    if boundary is None:
        raise ValueError("Boundary geometry is missing. Please rerun terrain analysis.")
    if not isinstance(boundary, Polygon):
        raise ValueError("Invalid boundary geometry supplied to KMZ export.")

    kml = simplekml.Kml()

    # Boundary polygon
    pol = kml.newpolygon(name="Park Boundary")
    pol.outerboundaryis = list(boundary.exterior.coords)
    pol.style.polystyle.color = simplekml.Color.changealphaint(40, simplekml.Color.blue)
    pol.style.linestyle.color = simplekml.Color.blue
    pol.style.linestyle.width = 2

    # Obstruction points
    for _, r in df[df.gradient >= 10].iterrows():
        p = kml.newpoint(coords=[(r.lon, r.lat)])
        p.style.iconstyle.color = simplekml.Color.red
        p.style.iconstyle.scale = 0.8

    bio = BytesIO()
    kml.savekmz(bio)
    bio.seek(0)
    return bio


# -------------------------------------------------
# Page config
# -------------------------------------------------
st.set_page_config(
    page_title="Terrain Analysis",
    page_icon="📊",
    layout="wide",
)

# Enforce Supabase authentication for direct page access.
from auth.supabase_auth import require_authentication
require_authentication()

from modules.project_io import render_project_io
render_project_io()
from modules.ad_slots import render_ad_slot
render_ad_slot("sidebar", compact=True)



# SURYA brand mark (kept inside the pages folder for portable deployment).
from pathlib import Path as _SuryaPath
_surya_logo = _SuryaPath(__file__).resolve().parent / "assets" / "surya_logo.png"
_surya_left, _surya_right = st.columns([1, 8], vertical_alignment="center")
with _surya_left:
    if _surya_logo.exists():
        st.image(str(_surya_logo), width=92)
with _surya_right:
    st.markdown("## SURYA – Solar Park Master Planner")

st.title("📊 Terrain Analysis")

# -------------------------------------------------
# PARK SESSION CHECK
# -------------------------------------------------
park = st.session_state.get("park_data")
if park is None:
    st.error("❌ Park session not initialised. Start from app.py")
    st.stop()

# -------------------------------------------------
# SIDEBAR – PARAMETERS
# -------------------------------------------------
st.sidebar.header("⚙️ Configuration")

grid_size = st.sidebar.slider("Grid Size (meters)", 25, 200, 50, 25)

gradient_threshold = st.sidebar.slider("Max Allowable Gradient (%)", 1, 20, 10)

elevation_source = st.sidebar.radio("Elevation Source", ["opentopo", "google", "survey"])

google_api_key = None
if elevation_source == "google":
    google_api_key = st.sidebar.text_input(
        "Google Elevation API Key", type="password"
    )

interp_method = st.sidebar.selectbox(
    "Interpolation Method", ["Cubic", "RBF", "IDW", "Kriging"]
)

# -------------------------------------------------
# FILE UPLOADS
# -------------------------------------------------
st.subheader("📂 Upload Boundary & Survey Files")

col_kmz, col_survey = st.columns(2)

with col_kmz:
    st.markdown("**KMZ Boundary** *(required)*")
    kmz_file = st.file_uploader("Upload KMZ boundary file", type=["kmz"], key="kmz_upload")
    if kmz_file is not None:
        st.session_state.kmz_bytes = kmz_file.read()

with col_survey:
    st.markdown("**DXF / DWG Physical Survey** *(optional)*")
    st.caption(
        "Upload the physical survey. The user-provided KMZ always controls the project boundary; "
        "verified survey XYZ controls elevations wherever survey coverage exists."
    )
    survey_file = st.file_uploader(
        "Upload survey file", type=["dxf", "dwg"], key="survey_upload"
    )

    survey_bytes = None
    if survey_file is not None:
        survey_bytes = survey_file.read()
        st.session_state.survey_bytes    = survey_bytes
        st.session_state.survey_filename = survey_file.name

    # CRS options — shown only when a survey file is loaded
    if st.session_state.get("survey_bytes"):
        st.markdown("**Survey CRS settings** *(fill in if coordinates look wrong)*")
        survey_utm_epsg = st.number_input(
            "UTM EPSG (0 = not specified)",
            min_value=0, max_value=99999,
            value=0, step=1,
            help="Enter the correct UTM EPSG for projected survey coordinates (Diksal: 32643). The UTM zone cannot reliably be inferred from easting/northing alone.",
        )
        survey_origin_lon = st.number_input(
            "Origin Longitude (for local CRS only)",
            value=0.0, format="%.6f",
            help="WGS84 longitude of the drawing's 0,0 origin. "
                 "Leave 0.0 if not a local coordinate file.",
        )
        survey_origin_lat = st.number_input(
            "Origin Latitude (for local CRS only)",
            value=0.0, format="%.6f",
        )
        st.session_state.survey_utm_epsg    = int(survey_utm_epsg) if survey_utm_epsg > 0 else None
        st.session_state.survey_origin_lon  = survey_origin_lon if survey_origin_lon != 0.0 else None
        st.session_state.survey_origin_lat  = survey_origin_lat if survey_origin_lat != 0.0 else None


# -------------------------------------------------
# SURVEY PARSE (preview, independent of full run)
# -------------------------------------------------
survey_result = None

if st.session_state.get("survey_bytes"):
    with st.expander("🗂️ Survey File Preview", expanded=False):
        if st.button("🔍 Parse Survey File"):
            with st.spinner("Parsing DXF/DWG …"):
                survey_result = parse_survey(
                    file_bytes=st.session_state.survey_bytes,
                    filename=st.session_state.get("survey_filename", "survey.dxf"),
                    utm_epsg=st.session_state.get("survey_utm_epsg"),
                    origin_lon=st.session_state.get("survey_origin_lon"),
                    origin_lat=st.session_state.get("survey_origin_lat"),
                )

            st.session_state.survey_result = survey_result

        # Display cached result
        if st.session_state.get("survey_result"):
            sr = st.session_state.survey_result

            for w in sr["warnings"]:
                st.warning(f"⚠️ {w}")

            crs_label = {"wgs84": "WGS84 degrees", "utm": "UTM metres", "local": "Local/arbitrary"}.get(
                sr["crs_detected"], sr["crs_detected"]
            )
            st.info(f"🧭 Detected CRS: **{crs_label}**")

            c1, c2, c3 = st.columns(3)
            c1.metric("Boundary extracted", "✅" if sr["boundary"] is not None else "❌")
            c2.metric("Spot heights", len(sr["spot_heights"]))
            c3.metric("Contour lines", len(sr["contours"]))

            if sr["boundary"] is not None:
                b = sr["boundary"]
                st.caption(
                    f"Boundary bbox: lon {b.bounds[0]:.5f}–{b.bounds[2]:.5f}, "
                    f"lat {b.bounds[1]:.5f}–{b.bounds[3]:.5f}"
                )

            if not sr["spot_heights"].empty:
                st.dataframe(
                    sr["spot_heights"].head(10).style.format({"lon": "{:.5f}", "lat": "{:.5f}", "elevation": "{:.2f}"}),
                    width="stretch",
                )

            # Coordinate validation + paired geographic / engineering views.
            if not sr["spot_heights"].empty:
                sh = sr["spot_heights"].copy()
                median_lon = float(sh["lon"].median())
                median_lat = float(sh["lat"].median())
                project_epsg = st.session_state.get("survey_utm_epsg") or get_utm_epsg_from_lonlat(median_lon, median_lat)
                try:
                    utm_sh, roundtrip_error_m = survey_coordinates_in_utm(sh, int(project_epsg))
                    c1, c2, c3 = st.columns(3)
                    c1.metric("WGS84 longitude", f"{sh.lon.min():.5f}° to {sh.lon.max():.5f}°")
                    c2.metric("WGS84 latitude", f"{sh.lat.min():.5f}° to {sh.lat.max():.5f}°")
                    c3.metric("UTM round-trip error", f"{roundtrip_error_m:.4f} m")
                    st.caption(
                        f"Coordinate check: source = WGS84 (EPSG:4326), engineering view = EPSG:{int(project_epsg)}; "
                        f"Easting {utm_sh.easting_m.min():,.2f}–{utm_sh.easting_m.max():,.2f} m; "
                        f"Northing {utm_sh.northing_m.min():,.2f}–{utm_sh.northing_m.max():,.2f} m. "
                        "The transformation is for display/engineering coordinates only; original survey values are retained."
                    )
                    geo_tab, utm_tab = st.tabs(["Geographic view – WGS84", "Engineering view – UTM metres"])
                    with geo_tab:
                        fig, ax = plt.subplots(figsize=(8, 5))
                        sc = ax.scatter(sh.lon, sh.lat, c=sh.elevation, cmap="terrain", s=18)
                        plt.colorbar(sc, ax=ax, label="Elevation (m)")
                        ax.set_title("Survey Spot Heights – Geographic Coordinates")
                        format_geographic_axes(ax)
                        ax.set_aspect("auto")
                        fig.tight_layout()
                        st.pyplot(fig)
                        plt.close(fig)
                    with utm_tab:
                        fig, ax = plt.subplots(figsize=(8, 5))
                        sc = ax.scatter(utm_sh.easting_m, utm_sh.northing_m, c=utm_sh.elevation, cmap="terrain", s=18)
                        plt.colorbar(sc, ax=ax, label="Elevation (m)")
                        ax.set_title(f"Survey Spot Heights – EPSG:{int(project_epsg)}")
                        ax.set_xlabel("Easting (m)")
                        ax.set_ylabel("Northing (m)")
                        ax.ticklabel_format(axis="both", style="plain", useOffset=False)
                        ax.set_aspect("equal", adjustable="datalim")
                        fig.tight_layout()
                        st.pyplot(fig)
                        plt.close(fig)
                except Exception as exc:
                    st.error(f"Could not validate/transform survey coordinates using EPSG:{project_epsg}: {exc}")


# -------------------------------------------------
# RUN TERRAIN ANALYSIS
# -------------------------------------------------
if st.button("▶ Run Terrain Analysis", type="primary"):

    if not st.session_state.get("kmz_bytes"):
        st.warning("Please upload a KMZ boundary file.")
        st.stop()

    progress = st.progress(0)
    status   = st.empty()

    # ------------------------------------------------------------------
    # Boundary source is always the user-provided KMZ.
    # Survey XYZ controls elevations only when Survey is selected.
    # ------------------------------------------------------------------
    survey_result = st.session_state.get("survey_result")
    boundary_override = None  # DXF boundary must never replace KMZ boundary.

    progress.progress(10)

    with st.spinner("Building terrain surface from selected elevation source …"):
        if elevation_source == "survey":
            if not survey_result or survey_result["spot_heights"].empty:
                st.error("Survey elevation source selected, but no parsed survey XYZ points are available. Parse the survey first.")
                st.stop()
            results = run_survey_controlled_terrain_analysis(
                kmz_bytes=st.session_state.kmz_bytes,
                grid_size=grid_size,
                gradient_threshold=gradient_threshold,
                survey_spot_heights=survey_result["spot_heights"],
                target_epsg=st.session_state.get("survey_utm_epsg"),
            )
        else:
            results = run_terrain_analysis(
                kmz_bytes=st.session_state.kmz_bytes,
                grid_size=grid_size,
                elevation_source=elevation_source,
                api_key=google_api_key,
                gradient_threshold=gradient_threshold,
                boundary_override=None,
                survey_spot_heights=None,
            )

    progress.progress(70)

    # Survey-controlled mode already creates a TIN surface clipped to the KMZ.
    # Do not run the legacy nearest-point overwrite or substitute DEM values.
    terrain_grid = results["terrain_grid"]

    progress.progress(80)

    # ------------------------------------------------------------------
    # FIX: compute UTM EPSG ONCE using final boundary
    # ------------------------------------------------------------------
    boundary = results["boundary"]  # always sourced from the user-provided KMZ

    if not isinstance(boundary, Polygon):
        st.error("❌ Invalid boundary geometry.")
        st.stop()

    centroid  = boundary.centroid
    utm_epsg  = get_utm_epsg_from_lonlat(centroid.x, centroid.y)

    # FIX: validate AFTER the single assignment (not before)
    if not (32601 <= utm_epsg <= 32760):
        st.error(f"❌ Could not derive a valid UTM EPSG (got {utm_epsg}). "
                 "Check the boundary coordinates.")
        st.stop()

    progress.progress(90)

    # ------------------------------------------------------------------
    # FIX: use the key "terrain_complete" consistently with app.py
    # ------------------------------------------------------------------
    # Terrain is the upstream source of truth. A new terrain run invalidates
    # all downstream park/site/layout outputs so stale geometry cannot survive.
    park["infrastructure_complete"] = False
    park["sites_generated"] = False
    park["sites_designed"] = 0
    park["site_layouts"] = {}
    park["park_complete"] = False
    for _key in ("sites", "sites_utm", "roads", "drainage", "electrical",
                  "facilities", "park_summary", "usable_area_utm", "usable_area_wgs",
                  "site_targets", "partition_hints"):
        park.pop(_key, None)

    park.update({
        "boundary":          boundary,                         # EPSG:4326 Polygon
        "utm_epsg":          utm_epsg,                         # int
        "terrain_grid":      terrain_grid,                     # pd.DataFrame
        "buildable_zones":   results["buildable_zones"],       # GeoDF EPSG:4326
        "restricted_zones":  results["restricted_zones"],      # GeoDF EPSG:4326
        "grid_size":         grid_size,
        "gradient_threshold": gradient_threshold,
        "terrain_complete":  True,                             # FIX: was "terrain_done"
        # Also store survey artefacts for downstream pages
        "survey_contours":   survey_result["contours"] if survey_result else [],
        "survey_spot_heights": survey_result["spot_heights"] if survey_result else pd.DataFrame(),
        "terrain_surface_report": results.get("surface_report"),
        "terrain_surface_metadata": results.get("surface_metadata"),
        "terrain_elevation_source": results.get("elevation_source", elevation_source),
        "terrain_boundary_source": results.get("boundary_source", "user-provided KMZ"),
        "terrain_surface_grid": results.get("surface_grid_all"),
    })

    progress.progress(100)
    status.empty()

    st.success("✅ Terrain analysis completed")
    st.success(f"🧭 UTM EPSG: {utm_epsg}")
    st.success("📐 Project boundary sourced from user-provided KMZ")
    if elevation_source == "survey":
        st.success("⛰️ Ground elevations controlled by parsed survey XYZ; DEM not blended or used for gap filling.")


# -------------------------------------------------
# DISPLAY RESULTS
# -------------------------------------------------
if park.get("terrain_complete"):                               # FIX: was "terrain_done"

    # FIX: always read df from session state, never rely on an in-scope variable
    df = park["terrain_grid"]

    st.markdown("---")
    st.header("📈 Terrain Summary")

    surface_report = park.get("terrain_surface_report")
    surface_metadata = park.get("terrain_surface_metadata") or {}
    if surface_report:
        with st.expander("Survey-controlled surface quality & source audit", expanded=True):
            st.write({
                "Boundary source": surface_report.get("boundary_source"),
                "Elevation source": surface_report.get("elevation_source"),
                "Survey points inside KMZ": surface_report.get("survey_points_inside_kmz"),
                "Survey points outside KMZ": surface_report.get("survey_points_outside_kmz"),
                "Unique survey points used": surface_report.get("survey_points_used"),
                "Supported grid coverage (%)": surface_report.get("survey_coverage_pct"),
                "NoData cells": surface_report.get("nodata_cells"),
                "Outlier candidates (retained)": surface_report.get("outlier_candidates"),
                "Preliminary only": surface_report.get("preliminary_only"),
            })
            for warning in surface_report.get("warnings", []):
                st.warning(warning)
            st.caption(
                "Source policy: KMZ controls boundary; survey XYZ controls elevations; "
                "unsupported survey cells remain NoData. DEM is not blended or substituted."
            )

    col1, col2, col3 = st.columns(3)
    col1.metric("Grid Points", len(df))
    col2.metric(
        "Elevation Range (m)",
        f"{df['elevation'].min():.1f} – {df['elevation'].max():.1f}",
    )
    col3.metric("Max Gradient (%)", f"{df['gradient'].max():.2f}")

    # -------------------------------------------------
    # AREA CLASSIFICATION  (FIX: suitability accounts for the FULL terrain grid)
    # -------------------------------------------------
    utm_epsg = park["utm_epsg"]
    boundary_utm = (
        gpd.GeoSeries([park["boundary"]], crs="EPSG:4326")
        .to_crs(epsg=utm_epsg)
        .iloc[0]
    )
    # The engineering/buildable polygon and the terrain suitability grid are
    # deliberately different measurements.  Page-1 suitability must account
    # for every terrain sample/grid cell, including cells that extend beyond
    # the boundary-clipped engineering polygon.
    #
    # Each terrain sample represents one nominal grid cell of
    # grid_size x grid_size metres.  Therefore the suitability areas are
    # calculated from the number of terrain cells in each gradient band.
    # This makes all three bands reconcile to the full terrain assessment
    # area rather than to the smaller boundary-clipped buildable area.
    try:
        grid_size_m = float(park["grid_size"])
        area_per_grid_cell_acres = (grid_size_m ** 2) / 4047.0

        # Classify every terrain grid sample into exactly one suitability band.
        ideal = df[df["gradient"] < 5]
        moderate = df[(df["gradient"] >= 5) & (df["gradient"] < 10)]
        challenging = df[df["gradient"] >= 10]

        ideal_area_acres = len(ideal) * area_per_grid_cell_acres
        moderate_area_acres = len(moderate) * area_per_grid_cell_acres
        challenging_area_acres = len(challenging) * area_per_grid_cell_acres
        terrain_assessed_area_acres = len(df) * area_per_grid_cell_acres

        suitability_total = (
            ideal_area_acres
            + moderate_area_acres
            + challenging_area_acres
        )

        st.markdown("---")
        st.subheader("🌧️ Preliminary Drainage Flow Screening")
        st.caption("D8 routing on a regular metre grid. Survey mode can rebuild a finer grid directly from the uploaded survey XYZ; unsupported survey areas remain NoData.")
        source_label = str(park.get("terrain_elevation_source", "unknown")).lower()
        surface_for_flow = park.get("terrain_surface_grid")
        if surface_for_flow is None or getattr(surface_for_flow, "empty", True):
            fallback = df.copy()
            if {"lon", "lat", "elevation"}.issubset(fallback.columns):
                _tx = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
                fallback["x_m"], fallback["y_m"] = _tx.transform(
                    fallback["lon"].to_numpy(), fallback["lat"].to_numpy()
                )
                fallback = fallback.rename(columns={"elevation": "elevation_m"})
                surface_for_flow = fallback[["x_m", "y_m", "elevation_m"]].copy()

        is_survey_source = source_label in {"survey", "survey xyz"}
        boundary_utm_for_flow = None
        try:
            boundary_utm_for_flow = gpd.GeoSeries([park["boundary"]], crs="EPSG:4326").to_crs(epsg=int(utm_epsg)).iloc[0]
        except Exception:
            pass
        st.caption(f"Elevation input: {source_label.upper()}. DEM results are preliminary. Survey flow grids are rebuilt from survey XYZ without DEM gap filling.")
        c1, c2, c3 = st.columns(3)
        with c1:
            if is_survey_source:
                flow_resolution_label = st.selectbox(
                    "Survey flow grid resolution", ["10 m", "20 m", "50 m"], index=0,
                    help="Interpolates a regular grid from survey XYZ inside the KMZ boundary. NoData gaps are preserved."
                )
                flow_resolution_m = int(flow_resolution_label.split()[0])
            else:
                st.selectbox("DEM flow grid resolution", [f"Existing terrain spacing ({float(park.get('grid_size', 50.0)):.0f} m)"], index=0,
                             help="DEM samples are projected and interpolated onto a regular grid at the existing terrain spacing. No finer DEM detail is fabricated.")
                flow_resolution_m = float(park.get("grid_size", 50.0))
        with c2:
            flow_threshold = st.select_slider(
                "Accumulation threshold (cells)", options=[2, 3, 5, 8, 12, 20], value=5,
                help="Lower values reveal more candidate flow links. Thresholds are cell-count based and change meaning with grid resolution."
            )
        with c3:
            minimum_path_length = st.select_slider(
                "Minimum connected network length (m)", options=[0, 20, 40, 60, 100, 150], value=20,
                help="Filters short disconnected flow-network fragments by their actual D8 link length."
            )

        if st.button("▶ Analyse preliminary flow paths", key="run_d8_flow_screening", type="primary"):
            try:
                if boundary_utm_for_flow is None:
                    raise ValueError("The KMZ project boundary is unavailable. Rerun Terrain Analysis first.")
                if is_survey_source:
                    survey_points_for_flow = park.get("survey_spot_heights")
                    if survey_points_for_flow is None or survey_points_for_flow.empty:
                        raise ValueError("Survey mode is selected, but parsed survey XYZ points are unavailable. Parse the survey file again.")
                    flow_surface_run, flow_surface_report, flow_surface_meta = build_survey_controlled_surface(
                        survey_points_wgs84=survey_points_for_flow,
                        kmz_boundary_utm=boundary_utm_for_flow,
                        target_epsg=int(utm_epsg),
                        grid_resolution_m=float(flow_resolution_m),
                        max_nearest_fill_distance_m=0.0,
                    )
                else:
                    # DEM samples originate in WGS84 grid coordinates. Rebuild
                    # a regular projected grid at the existing terrain spacing;
                    # this avoids routing on scattered UTM coordinates.
                    _dem_source = df.copy()
                    if not {"lon", "lat", "elevation"}.issubset(_dem_source.columns):
                        raise ValueError("Terrain samples do not contain lon/lat/elevation columns.")
                    _dem_tx = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
                    _dem_source["x_m"], _dem_source["y_m"] = _dem_tx.transform(
                        _dem_source["lon"].to_numpy(dtype=float), _dem_source["lat"].to_numpy(dtype=float)
                    )
                    _dem_source = _dem_source.rename(columns={"elevation": "elevation_m"})
                    flow_resolution_m = float(park.get("grid_size", 50.0))
                    flow_surface_run = build_regular_dem_flow_grid(
                        _dem_source[["x_m", "y_m", "elevation_m"]],
                        boundary_utm_for_flow,
                        flow_resolution_m,
                    )
                    flow_surface_report = None
                    flow_surface_meta = {"elevation_source": source_label, "preliminary_only": True,
                                          "interpolation": "linear TIN on existing DEM terrain samples; no nearest gap fill"}

                flow_df, flow_summary, terminal_df = run_d8_flow_screening(
                    flow_surface_run[["x_m", "y_m", "elevation_m"]],
                    int(flow_threshold),
                    minimum_path_length_m=float(minimum_path_length),
                    boundary=boundary_utm_for_flow,
                    include_terminals=True,
                    grid_resolution_m=float(flow_resolution_m),
                )
                if flow_surface_report is not None:
                    flow_summary["survey_coverage_pct"] = flow_surface_report.survey_coverage_pct
                    flow_summary["survey_points_inside_kmz"] = flow_surface_report.survey_points_inside_kmz
                    flow_summary["survey_points_outside_kmz"] = flow_surface_report.survey_points_outside_kmz
                    flow_summary["outlier_candidates_retained"] = flow_surface_report.outlier_candidates
                    flow_summary["survey_surface_warnings"] = list(flow_surface_report.warnings)
                else:
                    flow_summary["survey_coverage_pct"] = None
                park["drainage_flow_grid"] = flow_df
                park["drainage_flow_surface"] = flow_surface_run
                park["drainage_flow_summary"] = flow_summary
                park["drainage_flow_terminals"] = terminal_df
                park["drainage_flow_threshold"] = int(flow_threshold)
                park["drainage_flow_resolution_m"] = flow_resolution_m
                park["drainage_minimum_path_length_m"] = float(minimum_path_length)
                park["drainage_flow_surface_meta"] = flow_surface_meta
                # A new EGL / flow grid invalidates any earlier grading and post-grade comparison.
                for _stale_key in (
                    "drainage_grading_surface", "drainage_grading_summary", "drainage_grading_mode",
                    "drainage_post_grading_flow_grid", "drainage_post_grading_flow_summary",
                    "drainage_post_grading_flow_terminals", "drainage_grading_comparison",
                ):
                    park.pop(_stale_key, None)
                st.success("Preliminary flow screening completed on the selected grid.")
            except Exception as exc:
                st.error(f"Flow screening could not be completed: {exc}")

        flow_df = park.get("drainage_flow_grid")
        flow_summary = park.get("drainage_flow_summary")
        flow_surface_plot = park.get("drainage_flow_surface")
        terminal_df = park.get("drainage_flow_terminals")
        if flow_df is not None and not flow_df.empty and flow_summary:
            # Report both support measures explicitly: cell-count percentage and
            # area-weighted coverage of the full KMZ polygon are not the same.
            _flow_area_stats = None
            try:
                _, _flow_area_stats = calculate_weighted_suitability(
                    flow_surface_plot[["x_m", "y_m", "elevation_m"]].copy(),
                    grid_resolution_m=float(flow_summary.get("grid_spacing_x_m", flow_resolution_m)),
                    boundary_geometry=boundary_utm_for_flow,
                    ideal_limit_pct=5.0,
                    moderate_limit_pct=10.0,
                )
                park["drainage_flow_area_summary"] = _flow_area_stats
            except Exception:
                _flow_area_stats = park.get("drainage_flow_area_summary")

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Supported cells", f"{flow_summary['supported_cells']:,}")
            m2.metric("Grid-cell support", f"{flow_summary.get('coverage_pct', 0):.1f}%")
            if _flow_area_stats:
                _area_label = "Area-weighted survey coverage" if is_survey_source else "Area-weighted terrain coverage"
                m3.metric(_area_label, f"{_flow_area_stats['supported_elevation_coverage_pct_of_kmz']:.1f}%")
            else:
                m3.metric("Area-weighted coverage", "—")
            m4.metric("Maximum accumulation", f"{flow_summary['maximum_accumulation_cells']:,} cells")
            m5.metric("Network components", f"{flow_summary['network_components']:,}")
            m6.metric("Longest connected network", f"{flow_summary['longest_network_component_m']:.0f} m")
            if flow_summary.get("survey_coverage_pct") is not None and flow_summary["survey_coverage_pct"] < 90:
                st.warning(
                    f"Grid-cell support is {flow_summary['coverage_pct']:.1f}% by cell count. "
                    "Area-weighted support is reported separately against the full KMZ area. "
                    "Blank areas are NoData; flow paths are not bridged across them."
                )
            if _flow_area_stats and _flow_area_stats.get("unrepresented_boundary_area_m2", 0) > 1.0:
                st.caption(
                    f"{_flow_area_stats['unrepresented_boundary_area_m2']:,.0f} m² "
                    f"({_flow_area_stats['unrepresented_boundary_area_acres']:.2f} acres) of the KMZ polygon "
                    "is not represented by the current in-boundary grid-centre lattice. This area is not treated "
                    "as surveyed NoData; it is outside the represented grid and is excluded from classification."
                )
            if flow_summary.get("survey_surface_warnings"):
                with st.expander("Survey coverage and quality notes"):
                    for _warning in flow_summary["survey_surface_warnings"]:
                        st.warning(_warning)

            fig, ax = plt.subplots(figsize=(10, 7))
            # Plot actual gridded elevations. NaN cells remain visibly blank.
            try:
                _pivot = flow_surface_plot.pivot(index="y_m", columns="x_m", values="elevation_m").sort_index().sort_index(axis=1)
                _px = _pivot.columns.to_numpy(dtype=float)
                _py = _pivot.index.to_numpy(dtype=float)
                _pz = np.ma.masked_invalid(_pivot.to_numpy(dtype=float))
                _mesh = ax.pcolormesh(_px, _py, _pz, shading="nearest", cmap="terrain", alpha=0.86, zorder=1)
                fig.colorbar(_mesh, ax=ax, label="Survey / terrain elevation (m)")
            except Exception:
                _valid_flow = flow_df.dropna(subset=["elevation_m"])
                ax.scatter(_valid_flow.x_m, _valid_flow.y_m, c=_valid_flow.elevation_m, s=12, cmap="terrain", zorder=1)

            _links = flow_df[flow_df.drainage_corridor_candidate].copy()
            _main_segments, _secondary_segments = [], []
            for _r in _links.itertuples(index=False):
                if not np.isfinite(_r.downstream_x_m) or not np.isfinite(_r.downstream_y_m):
                    continue
                _segment = [(_r.x_m, _r.y_m), (_r.downstream_x_m, _r.downstream_y_m)]
                if _r.main_drain_link:
                    _main_segments.append(_segment)
                elif _r.secondary_drain_link:
                    _secondary_segments.append(_segment)
            if _secondary_segments:
                ax.add_collection(LineCollection(_secondary_segments, colors="#e67e22", linewidths=1.5, label="Secondary flow corridor", zorder=4))
            if _main_segments:
                ax.add_collection(LineCollection(_main_segments, colors="#c62828", linewidths=2.8, label="Main flow corridor", zorder=5))

            # Terminal points are not declared as outfalls; they are candidates for field review.
            if terminal_df is not None and not terminal_df.empty:
                _edge = terminal_df[terminal_df.terminal_type == "boundary_edge_candidate"]
                _inner = terminal_df[terminal_df.terminal_type == "interior_sink_or_coverage_stop"]
                if not _edge.empty:
                    ax.scatter(_edge.x_m, _edge.y_m, marker="s", s=42, c="#1565c0", edgecolors="white", label="Boundary-edge candidate (verify outfall)", zorder=7)
                if not _inner.empty:
                    ax.scatter(_inner.x_m, _inner.y_m, marker="x", s=38, c="#7b1fa2", label="Interior sink / coverage stop", zorder=7)

            try:
                _bx, _by = boundary_utm_for_flow.exterior.xy
                ax.plot(_bx, _by, color="black", linewidth=1.5, label="KMZ project boundary", zorder=8)
            except Exception:
                pass
            ax.set_aspect("equal", adjustable="datalim")
            ax.set_xlabel("UTM Easting (m)")
            ax.set_ylabel("UTM Northing (m)")
            _res_txt = f"{flow_summary['grid_spacing_x_m']:.0f} m grid"
            ax.set_title(f"Preliminary D8 drainage network – {_res_txt}")
            ax.ticklabel_format(axis="both", style="plain", useOffset=False)
            ax.legend(loc="best", fontsize=8)
            fig.tight_layout()
            st.pyplot(fig)
            plt.close(fig)

            with st.expander("Flow network method and limitations"):
                st.write(flow_summary)
                st.caption("Main links are cells with accumulation at least five times the selected threshold. Other retained links are shown as secondary. Boundary-edge candidates are not confirmed outfalls. Internal terminals may be real depressions or survey-coverage stops.")
            st.download_button(
                "Download flow screening CSV",
                flow_df.to_csv(index=False).encode("utf-8"),
                file_name="drainage_flow_screening.csv",
                mime="text/csv",
            )

            # -------------------------------------------------------------
            # Stage 4.3 – Integrated preliminary grading + cut/fill + drainage
            # -------------------------------------------------------------
            st.markdown("---")
            st.subheader("🏗️ Grading, Cut-Fill & Post-Grading Drainage")
            st.caption(
                "Create a preliminary proposed finished-ground level (FGL) from the supported existing-ground level (EGL). "
                "The main D8 corridor buffer is protected by default. NoData cells remain excluded. "
                "Minimum grading applies a gentle 10% adjustment toward the drainage-led target; depth caps limit the concept earthwork."
            )
            grade_c1, grade_c2, grade_c3 = st.columns(3)
            with grade_c1:
                grading_mode = st.selectbox(
                    "Grading strategy",
                    ["Minimum grading", "Balanced cut & fill", "Platform grading"],
                    index=0,
                    key="stage43_grading_mode",
                    help=("Minimum grading retains most natural terrain. Balanced mode shifts the drainage-led target to "
                          "reduce net import/export. Platform mode creates a broad plane using the dominant D8 fall direction."),
                )
            with grade_c2:
                target_grading_slope = st.slider(
                    "Target grading slope (%)", min_value=0.2, max_value=2.0, value=0.5, step=0.1,
                    key="stage43_target_slope",
                    help="Preliminary target fall used to form the grading concept; not a statutory minimum or approved design slope.",
                )
            with grade_c3:
                drainage_buffer = st.select_slider(
                    "Protected drainage corridor width (m)", options=[0, 5, 10, 15, 20, 30, 40], value=10,
                    key="stage43_drainage_buffer",
                    help="Cells within this distance of the current D8 corridor remain at existing ground level in this grading pass.",
                )

            protected_corridor_scope = st.selectbox(
                "Drainage corridor protection",
                ["Main flow corridors", "All identified flow corridors"],
                index=0,
                key="stage43_corridor_scope",
                help="Main corridors are protected by default. Protecting all secondary links can leave a large share of the site untouched by grading.",
            )

            grade_c4, grade_c5, grade_c6 = st.columns(3)
            with grade_c4:
                max_cut_depth = st.number_input(
                    "Maximum cut depth (m)", min_value=0.1, max_value=10.0, value=0.5, step=0.1,
                    key="stage43_max_cut",
                )
            with grade_c5:
                max_fill_depth = st.number_input(
                    "Maximum fill depth (m)", min_value=0.1, max_value=10.0, value=0.5, step=0.1,
                    key="stage43_max_fill",
                )
            with grade_c6:
                _platform_default = float(np.nanmedian(flow_surface_plot.elevation_m))
                if grading_mode == "Platform grading":
                    platform_level = st.number_input(
                        "Platform reference level (m)", value=round(_platform_default, 2), step=0.1,
                        key="stage43_platform_level",
                    )
                else:
                    platform_level = None
                    st.metric("EGL reference median", f"{_platform_default:.2f} m")

            cut_reuse_efficiency = st.slider(
                "Assumed reusable fraction of excavated cut (%)", min_value=0, max_value=100,
                value=80, step=5, key="stage431_cut_reuse_pct",
                help="Planning assumption only. Actual reuse depends on geotechnical soil suitability, moisture, compaction and shrink/swell tests.",
            )

            if st.button("▶ Generate proposed grading surface and recalculate drainage", key="run_stage43_grading", type="primary"):
                try:
                    grading_surface, grading_summary = design_finished_surface(
                        existing_surface=flow_surface_plot[["x_m", "y_m", "elevation_m"]].copy(),
                        flow_network=flow_df,
                        grid_resolution_m=float(flow_resolution_m),
                        grading_mode=grading_mode,
                        target_slope_percent=float(target_grading_slope),
                        drainage_buffer_m=float(drainage_buffer),
                        max_cut_m=float(max_cut_depth),
                        max_fill_m=float(max_fill_depth),
                        platform_level_m=platform_level,
                        protected_corridor_scope=protected_corridor_scope,
                        boundary_geometry=boundary_utm_for_flow,
                        cut_reuse_efficiency_pct=float(cut_reuse_efficiency),
                    )
                    proposed_flow_input = grading_surface[["x_m", "y_m", "finished_elevation_m"]].rename(
                        columns={"finished_elevation_m": "elevation_m"}
                    )
                    proposed_flow, proposed_flow_summary, proposed_terminals = run_d8_flow_screening(
                        proposed_flow_input,
                        accumulation_threshold_cells=int(flow_threshold),
                        minimum_path_length_m=float(minimum_path_length),
                        boundary=boundary_utm_for_flow,
                        include_terminals=True,
                        grid_resolution_m=float(flow_resolution_m),
                    )
                    park["drainage_grading_surface"] = grading_surface
                    park["drainage_grading_summary"] = grading_summary
                    park["drainage_grading_mode"] = grading_mode
                    park["drainage_post_grading_flow_grid"] = proposed_flow
                    park["drainage_post_grading_flow_summary"] = proposed_flow_summary
                    park["drainage_post_grading_flow_terminals"] = proposed_terminals
                    st.success("Proposed FGL generated and D8 drainage recalculated on the proposed surface.")
                except Exception as exc:
                    st.error(f"Grading analysis could not be completed: {exc}")

            if st.button("⚖️ Compare all three grading strategies on the same grid", key="run_stage431_compare"):
                try:
                    _comparison_rows = []
                    _comparison_surface = flow_surface_plot[["x_m", "y_m", "elevation_m"]].copy()
                    _comparison_platform_level = float(np.nanmedian(_comparison_surface.elevation_m))
                    for _mode in ["Minimum grading", "Balanced cut & fill", "Platform grading"]:
                        _candidate_fgl, _candidate_summary = design_finished_surface(
                            existing_surface=_comparison_surface,
                            flow_network=flow_df,
                            grid_resolution_m=float(flow_resolution_m),
                            grading_mode=_mode,
                            target_slope_percent=float(target_grading_slope),
                            drainage_buffer_m=float(drainage_buffer),
                            max_cut_m=float(max_cut_depth),
                            max_fill_m=float(max_fill_depth),
                            platform_level_m=_comparison_platform_level if _mode == "Platform grading" else None,
                            protected_corridor_scope=protected_corridor_scope,
                            boundary_geometry=boundary_utm_for_flow,
                            cut_reuse_efficiency_pct=float(cut_reuse_efficiency),
                        )
                        _candidate_flow_input = _candidate_fgl[["x_m", "y_m", "finished_elevation_m"]].rename(
                            columns={"finished_elevation_m": "elevation_m"}
                        )
                        _, _candidate_flow_summary, _ = run_d8_flow_screening(
                            _candidate_flow_input,
                            accumulation_threshold_cells=int(flow_threshold),
                            minimum_path_length_m=float(minimum_path_length),
                            boundary=boundary_utm_for_flow,
                            include_terminals=True,
                            grid_resolution_m=float(flow_resolution_m),
                        )
                        _comparison_rows.append({
                            "Strategy": _mode,
                            "Cut (m³)": _candidate_summary["cut_volume_m3"],
                            "Fill (m³)": _candidate_summary["fill_volume_m3"],
                            "Net fill − cut (m³)": _candidate_summary["net_fill_minus_cut_m3"],
                            "Potential on-site reuse (m³)": _candidate_summary["potential_on_site_reuse_m3"],
                            "Indicative imported fill (m³)": _candidate_summary["indicative_imported_fill_m3"],
                            "Reusable cut surplus (m³)": _candidate_summary["indicative_reusable_cut_surplus_m3"],
                            "Cut not assumed reusable (m³)": _candidate_summary["cut_not_assumed_reusable_m3"],
                            "Graded area (m²)": _candidate_summary["graded_area_m2"],
                            "Max cut (m)": _candidate_summary["maximum_cut_m"],
                            "Max fill (m)": _candidate_summary["maximum_fill_m"],
                            "D8 links": _candidate_flow_summary["network_link_count"],
                            "D8 components": _candidate_flow_summary["network_components"],
                            "Internal terminals": _candidate_flow_summary["interior_sink_or_coverage_stops"],
                            "Boundary candidates": _candidate_flow_summary["boundary_edge_candidates"],
                        })
                    park["drainage_grading_comparison"] = pd.DataFrame(_comparison_rows)
                    park["drainage_grading_comparison_assumptions"] = {
                        "grid_resolution_m": float(flow_resolution_m),
                        "threshold_cells": int(flow_threshold),
                        "minimum_path_length_m": float(minimum_path_length),
                        "reuse_pct": float(cut_reuse_efficiency),
                        "platform_reference_level_m": _comparison_platform_level,
                    }
                    st.success("All three grading strategies compared using the same EGL grid, boundary, drainage protection and D8 settings.")
                except Exception as exc:
                    st.error(f"Grading strategy comparison could not be completed: {exc}")

            _strategy_comparison = park.get("drainage_grading_comparison")
            if isinstance(_strategy_comparison, pd.DataFrame) and not _strategy_comparison.empty:
                st.markdown("#### Compare grading alternatives")
                st.dataframe(_strategy_comparison, width="stretch", hide_index=True)
                _cmp_assumptions = park.get("drainage_grading_comparison_assumptions", {})
                st.caption(
                    "All strategies use the same EGL grid, boundary, corridor protection and D8 settings. "
                    f"Comparison assumptions: { _cmp_assumptions.get('grid_resolution_m', '—') } m grid, "
                    f"{ _cmp_assumptions.get('reuse_pct', '—') }% assumed cut reuse, "
                    f"platform reference { _cmp_assumptions.get('platform_reference_level_m', '—') } m. "
                    "This is a transparent set of indicators, not an automatic winner score. Review drainage terminals and boundary candidates alongside earthwork quantities."
                )
                st.download_button(
                    "Download grading strategy comparison CSV",
                    _strategy_comparison.to_csv(index=False).encode("utf-8"),
                    file_name="grading_strategy_comparison.csv",
                    mime="text/csv",
                )

            grading_surface = park.get("drainage_grading_surface")
            grading_summary = park.get("drainage_grading_summary")
            proposed_flow = park.get("drainage_post_grading_flow_grid")
            proposed_flow_summary = park.get("drainage_post_grading_flow_summary")
            proposed_terminals = park.get("drainage_post_grading_flow_terminals")
            if grading_surface is not None and not grading_surface.empty and grading_summary:
                st.markdown("#### Preliminary cut-and-fill results")
                gm1, gm2, gm3, gm4 = st.columns(4)
                gm1.metric("Cut volume", f"{grading_summary['cut_volume_m3']:,.0f} m³")
                gm2.metric("Fill volume", f"{grading_summary['fill_volume_m3']:,.0f} m³")
                gm3.metric("Net fill − cut", f"{grading_summary['net_fill_minus_cut_m3']:+,.0f} m³")
                gm4.metric("Graded area", f"{grading_summary['graded_area_m2']:,.0f} m²")
                em1, em2, em3, em4, em5 = st.columns(5)
                em1.metric("Potential reusable cut", f"{grading_summary['potential_reusable_cut_m3']:,.0f} m³")
                em2.metric("Potential on-site reuse", f"{grading_summary['potential_on_site_reuse_m3']:,.0f} m³")
                em3.metric("Indicative imported fill", f"{grading_summary['indicative_imported_fill_m3']:,.0f} m³")
                em4.metric("Reusable cut surplus", f"{grading_summary['indicative_reusable_cut_surplus_m3']:,.0f} m³")
                em5.metric("Cut not assumed reusable", f"{grading_summary['cut_not_assumed_reusable_m3']:,.0f} m³")
                _grading_coverage_caption = (
                    f"Supported grid cells: {grading_summary.get('supported_cell_count_pct', grading_summary['survey_coverage_pct']):.1f}%"
                )
                if grading_summary.get("supported_area_coverage_pct_of_kmz") is not None:
                    _grading_coverage_caption += (
                        f" | Supported area of KMZ: {grading_summary['supported_area_coverage_pct_of_kmz']:.1f}%"
                    )
                st.caption(
                    f"Mode: {grading_summary['grading_mode']} | {_grading_coverage_caption} | "
                    f"Protected corridor cells: {grading_summary['protected_drainage_cells']:,} | "
                    f"Maximum cut: {grading_summary['maximum_cut_m']:.2f} m | Maximum fill: {grading_summary['maximum_fill_m']:.2f} m | "
                    f"Assumed cut reuse: {grading_summary['cut_reuse_efficiency_pct']:.0f}%"
                )
                if grading_summary.get("unrepresented_boundary_area_m2") is not None and grading_summary['unrepresented_boundary_area_m2'] > 1.0:
                    st.caption(
                        f"KMZ area not represented by in-boundary grid centres: "
                        f"{grading_summary['unrepresented_boundary_area_m2']:,.0f} m². "
                        "This edge area is reported separately and is not assigned fabricated elevations."
                    )
                if grading_summary["nodata_cells"]:
                    st.warning(
                        f"{grading_summary['nodata_cells']:,} cells are NoData and were excluded from grading and earthwork quantities. "
                        "No elevations were fabricated across unsupported survey gaps."
                    )

                _grade_pivot = grading_surface.pivot(index="y_m", columns="x_m", values="level_change_m").sort_index().sort_index(axis=1)
                _grade_x = _grade_pivot.columns.to_numpy(dtype=float)
                _grade_y = _grade_pivot.index.to_numpy(dtype=float)
                _grade_z = np.ma.masked_invalid(_grade_pivot.to_numpy(dtype=float))
                _grade_fig, _grade_ax = plt.subplots(figsize=(10, 6))
                _grade_mesh = _grade_ax.pcolormesh(_grade_x, _grade_y, _grade_z, shading="nearest", cmap="RdYlBu_r", vmin=-max(float(max_cut_depth), float(max_fill_depth)), vmax=max(float(max_cut_depth), float(max_fill_depth)))
                _grade_fig.colorbar(_grade_mesh, ax=_grade_ax, label="FGL − EGL (m): cut negative / fill positive")
                try:
                    _gbx, _gby = boundary_utm_for_flow.exterior.xy
                    _grade_ax.plot(_gbx, _gby, color="black", linewidth=1.5, label="KMZ project boundary")
                except Exception:
                    pass
                _protected = grading_surface[grading_surface.drainage_buffer_protected.astype(bool)]
                if not _protected.empty:
                    _grade_ax.scatter(_protected.x_m, _protected.y_m, s=2, c="#263238", alpha=0.35, label="Protected drainage buffer")
                _grade_ax.set_aspect("equal", adjustable="datalim")
                _grade_ax.set_xlabel("UTM Easting (m)")
                _grade_ax.set_ylabel("UTM Northing (m)")
                _grade_ax.set_title("Preliminary cut-and-fill map")
                _grade_ax.ticklabel_format(axis="both", style="plain", useOffset=False)
                _grade_ax.legend(loc="best", fontsize=8)
                _grade_fig.tight_layout()
                st.pyplot(_grade_fig)
                plt.close(_grade_fig)

                if proposed_flow_summary:
                    st.markdown("#### Drainage comparison – existing ground vs proposed FGL")
                    before_sinks = int(flow_summary.get("interior_sink_or_coverage_stops", 0))
                    after_sinks = int(proposed_flow_summary.get("interior_sink_or_coverage_stops", 0))
                    before_edges = int(flow_summary.get("boundary_edge_candidates", 0))
                    after_edges = int(proposed_flow_summary.get("boundary_edge_candidates", 0))
                    dc1, dc2, dc3, dc4 = st.columns(4)
                    dc1.metric("Network links", f"{proposed_flow_summary['network_link_count']:,}", delta=f"{proposed_flow_summary['network_link_count']-flow_summary['network_link_count']:+,} vs EGL")
                    dc2.metric("Network components", f"{proposed_flow_summary['network_components']:,}", delta=f"{proposed_flow_summary['network_components']-flow_summary['network_components']:+,} vs EGL")
                    dc3.metric("Interior sink / coverage stops", f"{after_sinks:,}", delta=f"{after_sinks-before_sinks:+,} vs EGL", delta_color="inverse")
                    dc4.metric("Boundary-edge candidates", f"{after_edges:,}", delta=f"{after_edges-before_edges:+,} vs EGL")
                    st.caption("These counts are screening indicators only. A higher boundary-edge count is not proof of a safe or approved outfall, and fewer internal terminals do not by themselves establish acceptable drainage.")

                    _after_links = proposed_flow[proposed_flow.drainage_corridor_candidate.astype(bool)].copy()
                    _after_segments = []
                    for _rr in _after_links.itertuples(index=False):
                        if np.isfinite(_rr.downstream_x_m) and np.isfinite(_rr.downstream_y_m):
                            _after_segments.append([(_rr.x_m, _rr.y_m), (_rr.downstream_x_m, _rr.downstream_y_m)])
                    _post_fig, _post_ax = plt.subplots(figsize=(10, 6))
                    _fgl_pivot = grading_surface.pivot(index="y_m", columns="x_m", values="finished_elevation_m").sort_index().sort_index(axis=1)
                    _fgl_mesh = _post_ax.pcolormesh(_fgl_pivot.columns.to_numpy(float), _fgl_pivot.index.to_numpy(float), np.ma.masked_invalid(_fgl_pivot.to_numpy(float)), shading="nearest", cmap="terrain", alpha=0.85)
                    _post_fig.colorbar(_fgl_mesh, ax=_post_ax, label="Proposed finished level (m)")
                    if _after_segments:
                        _post_ax.add_collection(LineCollection(_after_segments, colors="#c62828", linewidths=1.5, label="Post-grading D8 corridors"))
                    try:
                        _abx, _aby = boundary_utm_for_flow.exterior.xy
                        _post_ax.plot(_abx, _aby, color="black", linewidth=1.4, label="KMZ project boundary")
                    except Exception:
                        pass
                    _post_ax.set_aspect("equal", adjustable="datalim")
                    _post_ax.set_xlabel("UTM Easting (m)")
                    _post_ax.set_ylabel("UTM Northing (m)")
                    _post_ax.set_title("Post-grading preliminary drainage network")
                    _post_ax.ticklabel_format(axis="both", style="plain", useOffset=False)
                    _post_ax.legend(loc="best", fontsize=8)
                    _post_fig.tight_layout()
                    st.pyplot(_post_fig)
                    plt.close(_post_fig)

                with st.expander("Grading method, quantities and limitations"):
                    st.write(grading_summary)
                    st.caption("Volumes are approximate supported-cell volumes only. They exclude topsoil stripping, bulking/shrinkage, compaction, benches, haul factors and detailed platform boundaries. Review FGL and rerun drainage before accepting any grading proposal.")
                st.download_button(
                    "Download proposed FGL and cut-fill grid CSV",
                    grading_surface.to_csv(index=False).encode("utf-8"),
                    file_name="proposed_finished_ground_cut_fill.csv",
                    mime="text/csv",
                )
                if proposed_flow is not None and not proposed_flow.empty:
                    st.download_button(
                        "Download post-grading drainage network CSV",
                        proposed_flow.to_csv(index=False).encode("utf-8"),
                        file_name="post_grading_drainage_network.csv",
                        mime="text/csv",
                    )

        _suitability_basis = f"Original terrain grid ({grid_size_m:.0f} m nominal cells)"
        _suitability_detail = None
        _active_flow_surface = park.get("drainage_flow_surface")
        _active_flow_resolution = park.get("drainage_flow_resolution_m")
        if _active_flow_surface is not None and not getattr(_active_flow_surface, "empty", True) and _active_flow_resolution:
            try:
                _weighted_suitability, _suitability_detail = calculate_weighted_suitability(
                    _active_flow_surface[["x_m", "y_m", "elevation_m"]].copy(),
                    grid_resolution_m=float(_active_flow_resolution),
                    boundary_geometry=boundary_utm,
                    ideal_limit_pct=5.0,
                    moderate_limit_pct=10.0,
                )
                ideal_area_acres = _suitability_detail["ideal_area_acres"]
                moderate_area_acres = _suitability_detail["moderate_area_acres"]
                challenging_area_acres = _suitability_detail["challenging_area_acres"]
                terrain_assessed_area_acres = _suitability_detail["classified_area_acres"]
                suitability_total = ideal_area_acres + moderate_area_acres + challenging_area_acres
                _suitability_basis = f"{float(_active_flow_resolution):.0f} m analysis grid; KMZ-boundary-weighted cell areas"
            except Exception as _weighted_suitability_error:
                st.warning(f"Fine-grid suitability calculation unavailable; showing the original terrain-grid summary. {_weighted_suitability_error}")

        st.subheader("📊 Site Suitability (Area)")
        st.caption(
            f"Classified terrain area: {terrain_assessed_area_acres:.2f} acres | Basis: {_suitability_basis}. "
            "Gradient bands are mutually exclusive and use the same active grid as drainage when available."
        )
        if _suitability_detail:
            su1, su2, su3, su4 = st.columns(4)
            su1.metric("KMZ boundary area", f"{_suitability_detail['boundary_area_acres']:.2f} acres")
            su2.metric("Represented grid area", f"{_suitability_detail['represented_grid_area_acres']:.2f} acres")
            su3.metric("Supported elevation area", f"{_suitability_detail['supported_elevation_coverage_pct_of_kmz']:.1f}% of KMZ")
            su4.metric("Classified gradient area", f"{_suitability_detail['classified_gradient_coverage_pct_of_kmz']:.1f}% of KMZ")
            if _suitability_detail["unclassified_gradient_cells"] or _suitability_detail["nodata_elevation_cells"]:
                st.warning(
                    f"{_suitability_detail['nodata_elevation_cells']} NoData elevation cells and "
                    f"{_suitability_detail['unclassified_gradient_cells']} unclassified gradient cells remain outside the suitability bands. "
                    "NoData and unclassified areas are not represented as challenging terrain."
                )
            if _suitability_detail.get("unrepresented_boundary_area_m2", 0) > 1.0:
                st.warning(
                    f"{_suitability_detail['unrepresented_boundary_area_m2']:,.0f} m² "
                    f"({_suitability_detail['unrepresented_boundary_area_acres']:.2f} acres) of the KMZ "
                    "boundary is not represented by the current grid-centre lattice. It is separate from "
                    "survey NoData and is not assigned a terrain class."
                )
            st.caption(_suitability_detail["area_method"])

        # Every grid cell is assigned to exactly one band, so this should
        # reconcile to the full terrain assessed area apart from FP rounding.
        if abs(suitability_total - terrain_assessed_area_acres) > 1e-9:
            st.warning(
                f"⚠️ Suitability reconciliation difference: "
                f"{suitability_total:.2f} ac vs "
                f"{terrain_assessed_area_acres:.2f} ac assessed."
            )
    except Exception as _area_err:
        st.warning(f"Suitability area calculation unavailable: {_area_err}")
        ideal_area_acres = moderate_area_acres = challenging_area_acres = 0.0
        terrain_assessed_area_acres = 0.0
    # Compatibility note: terrain suitability uses full nominal grid-cell area.
    # Compatibility note: terrain suitability uses full nominal grid-cell area.
    # Compatibility note: terrain suitability uses full nominal grid-cell area.
    # Compatibility note: terrain suitability uses full nominal grid-cell area.
    # Compatibility note: terrain suitability uses full nominal grid-cell area.

    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 Ideal (<5%)",        f"{ideal_area_acres:.2f} acres")
    c2.metric("🟠 Moderate (5–10%)",   f"{moderate_area_acres:.2f} acres")
    c3.metric("🔴 Challenging (>10%)", f"{challenging_area_acres:.2f} acres")

    # -------------------------------------------------
    # SCATTER MAPS
    # -------------------------------------------------
    st.header("🗺️ Elevation & Gradient Maps")

    c1, c2 = st.columns(2)
    with c1:
        fig, ax = plt.subplots()
        sc = ax.scatter(df.lon, df.lat, c=df.elevation, cmap="terrain")
        plt.colorbar(sc, ax=ax, label="Elevation (m)")
        ax.set_title("Elevation")
        st.pyplot(fig)
        plt.close()

    with c2:
        fig, ax = plt.subplots()
        sc = ax.scatter(df.lon, df.lat, c=df.gradient, cmap="RdYlGn_r")
        plt.colorbar(sc, ax=ax, label="Gradient (%)")
        ax.set_title("Gradient")
        st.pyplot(fig)
        plt.close()

    # -------------------------------------------------
    # SURVEY CONTOURS OVERLAY (shown when survey was uploaded)
    # -------------------------------------------------
    contours = park.get("survey_contours", [])
    if contours:
        st.subheader("📏 Survey Contour Lines")
        fig, ax = plt.subplots()
        sc = ax.scatter(df.lon, df.lat, c=df.elevation, cmap="terrain", alpha=0.4, s=8)
        for line in contours:
            xs, ys = line.xy
            ax.plot(xs, ys, color="saddlebrown", linewidth=0.8, alpha=0.7)
        plt.colorbar(sc, ax=ax, label="Elevation (m)")
        ax.set_title("Satellite Elevation + Survey Contours")
        st.pyplot(fig)
        plt.close()

    # -------------------------------------------------
    # CONTOUR MAPS
    # -------------------------------------------------
    st.header("⛰️ Terrain Contour Maps")

    lon_grid = np.linspace(df.lon.min(), df.lon.max(), 100)
    lat_grid = np.linspace(df.lat.min(), df.lat.max(), 100)
    lon_m, lat_m = np.meshgrid(lon_grid, lat_grid)
    pts = np.column_stack((df.lon, df.lat))

    if interp_method == "RBF":
        elev_grid = Rbf(df.lon, df.lat, df.elevation)(lon_m, lat_m)
    else:
        method = "cubic" if interp_method in ["Cubic", "Kriging"] else "linear"
        elev_grid = griddata(pts, df.elevation.values, (lon_m, lat_m), method=method)

    # Gradient is a derived engineering quantity.  Do not use cubic/RBF here:
    # those interpolators can overshoot the measured gradient range (e.g. a
    # 14% observed maximum becoming a 17.5% contour).  Linear interpolation
    # preserves the local trend without creating artificial extrema.
    grad_grid = griddata(pts, df.gradient.values, (lon_m, lat_m), method="linear")
    if np.isnan(grad_grid).any():
        nearest_grad = griddata(pts, df.gradient.values, (lon_m, lat_m), method="nearest")
        grad_grid = np.where(np.isnan(grad_grid), nearest_grad, grad_grid)
    grad_grid = np.clip(grad_grid, float(df.gradient.min()), float(df.gradient.max()))

    c1, c2 = st.columns(2)
    with c1:
        fig, ax = plt.subplots()
        cf = ax.contourf(lon_m, lat_m, elev_grid, cmap="terrain")
        plt.colorbar(cf, ax=ax, label="Elevation (m)")
        ax.set_title("Elevation Contours")
        st.pyplot(fig)
        plt.close()

    with c2:
        fig, ax = plt.subplots()
        cf = ax.contourf(lon_m, lat_m, grad_grid, cmap="RdYlGn_r")
        plt.colorbar(cf, ax=ax, label="Gradient (%)")
        ax.set_title("Gradient Contours")
        st.pyplot(fig)
        plt.close()


# -------------------------------------------------
# EXPORT
# -------------------------------------------------
st.markdown("---")
st.header("⬇️ Export Data")

if not park.get("terrain_complete"):                           # FIX: was "terrain_done"
    st.info("👆 Run terrain analysis to enable export options.")
else:
    # FIX: always pull df from session state — never assume it is in scope
    df       = park["terrain_grid"]
    boundary = park["boundary"]

    col_e1, col_e2 = st.columns(2)

    with col_e1:
        try:
            kmz_out = generate_obstruction_kmz(df, boundary)
            st.download_button(
                "🗺️ Download Obstruction KMZ",
                kmz_out,
                file_name="terrain_obstructions.kmz",
                mime="application/vnd.google-earth.kmz",
            )
        except Exception as e:
            st.error("❌ Unable to generate KMZ export")
            st.exception(e)

    with col_e2:
        csv_buf = df.to_csv(index=False).encode()
        st.download_button(
            "📄 Download Terrain Grid CSV",
            csv_buf,
            file_name="terrain_grid.csv",
            mime="text/csv",
        )

    # Survey spot heights export
    sh = park.get("survey_spot_heights")
    if sh is not None and not sh.empty:
        sh_csv = sh.to_csv(index=False).encode()
        st.download_button(
            "📍 Download Survey Spot Heights CSV",
            sh_csv,
            file_name="survey_spot_heights.csv",
            mime="text/csv",
        )
