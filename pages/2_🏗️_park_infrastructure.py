import streamlit as st
import geopandas as gpd
import pandas as pd
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
from shapely.geometry import Point, LineString, shape
from shapely.ops import nearest_points
from pyproj import Transformer
import logging
import json

from modules.park_infrastructure.park_generator import (
    ParkInfrastructureGenerator,
    export_site_kmz
)
from modules.drainage.page2_overlay import (
    build_grading_overlay_gdf,
    build_drainage_overlay_gdf,
    build_terminal_overlay_gdf,
)
from modules.drainage.dashboard import (
    build_drainage_dashboard_payload,
    build_road_crossing_register,
    merge_road_crossing_registers,
)
from modules.drainage.main_collector import (
    build_outfall_candidate_register,
    design_spine_aligned_collector,
    kmz_receiving_feature_options,
    suggest_outfall_point,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------------------------------------
# Page config
# -------------------------------------------------
st.set_page_config(
    page_title="Park Infrastructure",
    page_icon="🏗️",
    layout="wide"
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

st.title("🏗️ Park Infrastructure Planning")

# -------------------------------------------------
# Session checks (STRICT but SAFE)
# -------------------------------------------------
if "park_data" not in st.session_state:
    st.error("Session not initialised. Start from app.py")
    st.stop()

park = st.session_state.park_data

# -------------------------------------------------
# Read PARTITION HINTS (safe, optional, Page-3 → Page-2)
# -------------------------------------------------
hints = park.get("partition_hints", {})

default_acres_per_mw = float(hints.get("acres_per_mw", 2.5))
default_min_site_mw = float(hints.get("min_site_ac_mw", 1.0))
default_max_site_mw = float(hints.get("max_site_ac_mw", 5.0))

if hints:
    st.info("ℹ️ Defaults below are prefilled from Page-3 capacity review.")

site_targets = park.get("site_targets")
if site_targets:
    st.success(
        f"🎯 **{len(site_targets)} per-site targets loaded from Page-3 Freeze.** "
        f"Click Generate to apply exact geometry. "
        f"Sites: {', '.join(f"{t['site_id']}={t['ac_mw']:.1f}MW" for t in site_targets)}"
    )


required = ["boundary", "buildable_zones", "utm_epsg"]
missing = [k for k in required if park.get(k) is None]

if missing:
    st.warning(
        "⚠️ Complete Terrain Analysis (Page-1) first. Missing: "
        + ", ".join(missing)
    )
    st.stop()

boundary = park["boundary"]
buildable_gdf = park["buildable_zones"]
restricted_gdf = park.get("restricted_zones")
utm_epsg = park["utm_epsg"]

# Page-2 may consume Page-1's post-grading D8 network as the source for its
# generated drainage channels. Prefer the post-grading network; otherwise use
# the existing-ground D8 network. This does not alter sites, roads or electricals.
_page2_drainage_flow_grid = park.get("drainage_post_grading_flow_grid")
if not isinstance(_page2_drainage_flow_grid, pd.DataFrame) or _page2_drainage_flow_grid.empty:
    _page2_drainage_flow_grid = park.get("drainage_flow_grid")
if not isinstance(_page2_drainage_flow_grid, pd.DataFrame) or _page2_drainage_flow_grid.empty:
    _page2_drainage_flow_grid = None

# Page-2 uses the complete terrain envelope (buildable + challenging zones)
# for continuous site planning; Page-1 suitability remains the terrain constraint
# classification for subsequent detailed layout decisions.

# -------------------------------------------------
# Controls
# -------------------------------------------------
st.subheader("🛠️ Infrastructure Parameters")

c1, c2, c3 = st.columns(3)

with c1:
    acres_per_mw = st.number_input(
        "Land Requirement (acres / MW AC)",
        value=default_acres_per_mw,
        step=0.1
    )

with c2:
    min_site_mw = st.number_input(
        "Minimum Site Size (MW AC)",
        value=default_min_site_mw,
        step=0.5
    )
    max_site_mw = st.number_input(
        "Maximum Site Size (MW AC)",
        value=default_max_site_mw,
        step=0.5
    )

with c3:
    centroid = boundary.centroid
    entry_lat = st.number_input(
        "Main Road Entry – Latitude",
        value=float(centroid.y),
        format="%.6f"
    )
    entry_lon = st.number_input(
        "Main Road Entry – Longitude",
        value=float(centroid.x),
        format="%.6f"
    )

# -------------------------------------------------
# SPINE ROAD PLACEMENT CONTROL
# -------------------------------------------------
st.subheader("🛣️ Spine Road Placement")

# Orientation — horizontal (E-W) or vertical (N-S)
spine_orientation = st.radio(
    "Spine orientation",
    ["↔️ Horizontal (E-W)", "↕️ Vertical (N-S)"],
    horizontal=True,
    help="Horizontal suits wide elongated parks; Vertical suits tall narrow parks."
)
spine_orientation_val = "vertical" if spine_orientation.startswith("↕️") else "horizontal"

spine_mode = st.radio(
    "Spine placement method",
    ["🤖 Auto (area-balanced)", "✏️ Manual override"],
    horizontal=True,
    help=(
        "Auto finds the Y position where north and south buildable areas are equal. "
        "Use Manual if the park has a natural road corridor, ridge, or entry from an unusual side."
    )
)

spine_bias = None  # None = auto in park_generator

if spine_mode == "✏️ Manual override":
    col_sl, col_info = st.columns([3, 1])
    with col_sl:
        if spine_orientation_val == "horizontal":
            spine_pct = st.slider(
                "Spine position (% from south boundary)",
                min_value=20, max_value=80, value=50, step=1,
                help="50% = centre. Lower = road south (more north land). Higher = road north (more south land)."
            )
            spine_bias = spine_pct / 100.0
            _spine_axis_label = f"{spine_pct}% from south"
            _spine_dir_hint   = ("⬆️ More north land" if spine_pct < 40
                                 else "⬇️ More south land" if spine_pct > 60
                                 else "↔️ Near centre")
        else:
            spine_pct = st.slider(
                "Spine position (% from west boundary)",
                min_value=20, max_value=80, value=50, step=1,
                help="50% = centre. Lower = road west (more east land). Higher = road east (more west land)."
            )
            spine_bias = spine_pct / 100.0
            _spine_axis_label = f"{spine_pct}% from west"
            _spine_dir_hint   = ("⬅️ More east land" if spine_pct < 40
                                 else "➡️ More west land" if spine_pct > 60
                                 else "↕️ Near centre")

    with col_info:
        st.metric("Spine at", _spine_axis_label)
        st.caption(_spine_dir_hint)

    # Live preview map
    try:
        from shapely.geometry import LineString as _LS
        _bnd_utm = gpd.GeoSeries([boundary], crs=4326).to_crs(epsg=utm_epsg).iloc[0]
        _minx, _miny, _maxx, _maxy = _bnd_utm.bounds

        if spine_orientation_val == "horizontal":
            _sv = _miny + (_maxy - _miny) * spine_bias
            _raw = _LS([(_minx - 1, _sv), (_maxx + 1, _sv)])
        else:
            _sv = _minx + (_maxx - _minx) * spine_bias
            _raw = _LS([(_sv, _miny - 1), (_sv, _maxy + 1)])

        # Clip to boundary for preview accuracy
        _clipped = _raw.intersection(_bnd_utm)
        _preview_spine = _clipped if not _clipped.is_empty else _raw
        if _preview_spine.geom_type == "MultiLineString":
            _preview_spine = max(_preview_spine.geoms, key=lambda g: g.length)

        _spine_wgs = gpd.GeoSeries([_preview_spine], crs=utm_epsg).to_crs(epsg=4326).iloc[0]

        import folium
        from streamlit_folium import st_folium as _st_folium
        _pm = folium.Map(location=[boundary.centroid.y, boundary.centroid.x],
                         zoom_start=15, control_scale=True)
        folium.Polygon(
            locations=[(lat, lon) for lon, lat in boundary.exterior.coords],
            color="black", weight=2, fill=False
        ).add_to(_pm)
        folium.PolyLine(
            locations=[(lat, lon) for lon, lat in _spine_wgs.coords],
            color="#e74c3c", weight=3, dash_array="8,4",
            tooltip=f"Spine preview ({_spine_axis_label})"
        ).add_to(_pm)
        st.caption("🔴 Dashed line = spine preview (clipped to boundary). Adjust until it sits in the corridor.")
        _st_folium(_pm, height=300, width=None, returned_objects=[])

    except Exception as _prev_err:
        st.caption(f"Preview unavailable: {_prev_err}")

else:
    st.caption("✅ Auto mode: spine placed at area-balance point after generation.")

if acres_per_mw <= 0:
    st.error("Land requirement must be greater than 0 acres/MW.")
if min_site_mw <= 0 or max_site_mw <= 0 or min_site_mw > max_site_mw:
    st.error("Invalid site-size limits: minimum must be > 0 and <= maximum.")

generate = st.button(
    "🚀 Generate Park Infrastructure",
    type="primary",
    disabled=(acres_per_mw <= 0 or min_site_mw <= 0 or max_site_mw <= 0 or min_site_mw > max_site_mw),
)

# -------------------------------------------------
# Generate infrastructure
# -------------------------------------------------
if generate:
    # A fresh generation starts from the current Page-3/site targets.
    # Any manual site deletions belong to the previous generated layout.
    park["deleted_site_ids"] = []
    # Do not leave stale Page-2 geometry visible if a regeneration fails.
    park["infrastructure_complete"] = False
    park["infrastructure_done"] = False
    # A changed spine/site-road geometry invalidates the previous collector design.
    park.pop("drainage_master_design", None)
    with st.spinner("Generating park infrastructure…"):
        try:
            entry_point = Point(entry_lon, entry_lat)

            gen = ParkInfrastructureGenerator()
            # Store params for Page-3 auto-trigger
            park["last_spine_bias"]        = spine_bias
            park["last_spine_orientation"] = spine_orientation_val
            park["entry_lat"]              = float(entry_lat)
            park["entry_lon"]              = float(entry_lon)

            infra = gen.generate_complete_infrastructure(
                park_boundary=boundary,
                buildable_zones=buildable_gdf,
                main_road_entry_point=entry_point,
                utm_epsg=utm_epsg,
                acres_per_mw=acres_per_mw,
                min_site_mw=min_site_mw,
                max_site_mw=max_site_mw,
                spine_bias=spine_bias,
                spine_orientation=spine_orientation_val,
                site_targets=park.get("site_targets"),
                restricted_zones=restricted_gdf,
                drainage_flow_grid=_page2_drainage_flow_grid,
            )

            # --- Persist (authoritative split) ---
            
            # Validate generated geometry before persisting it.
            _sites_utm = infra.get("sites_utm")
            _usable_utm = infra.get("usable_area_utm")
            if _sites_utm is None or _sites_utm.empty:
                raise ValueError("Infrastructure generation returned no site geometry.")
            if _usable_utm is None or _usable_utm.is_empty:
                raise ValueError("Infrastructure generation returned no usable-area geometry.")

            _assigned_area = _sites_utm.geometry.union_all().area
            _sum_area = _sites_utm.geometry.area.sum()
            _usable_area = _usable_utm.area
            _coverage = _assigned_area / _usable_area if _usable_area else 0.0
            _overlap = max(0.0, _sum_area - _assigned_area)
            _residual = max(0.0, _usable_area - _assigned_area)

            if _overlap > 1.0:
                raise ValueError(f"Site geometry overlap is {_overlap/4047:.3f} acres; generation rejected.")
            if _coverage < 0.95:
                raise ValueError(
                    f"Only {_coverage*100:.1f}% of engineering-usable land is assigned to sites "
                    f"({_residual/4047:.2f} acres residual). Adjust the spine or site constraints."
                )

            logger.info(
                f"Page-2 validation passed: coverage={_coverage*100:.2f}%, "
                f"residual={_residual/4047:.2f} ac, overlap={_overlap/4047:.4f} ac"
            )

            park["sites"] = infra["sites"]
            park["sites_utm"] = infra["sites_utm"] 

            #park["roads_utm"] = infra["roads_utm"]
            park["roads"] = infra["roads"]
            park["drainage"] = infra["drainage"]

            #park["electrical_utm"] = infra["electrical_utm"]
            park["electrical"] = infra["electrical"]
     
          

            park["facilities"] = infra["facilities"]

            park["usable_area_utm"] = infra.get("usable_area_utm")
            park["usable_area_wgs"] = infra.get("usable_area_wgs")
            park["spine_utm"] = infra.get("spine_utm")
            park["land_reconciliation"] = infra.get("land_reconciliation", {})

            park["park_summary"] = dict(infra["infra_summary"])
            park["park_summary"].update({
                "site_count": int(len(_sites_utm)),
                "site_capacity_mw": round(float(_sites_utm["ac_mw"].sum()), 2),
                "usable_area_acres": round(float(_usable_area / 4047.0), 2),
                "site_coverage_pct": round(float(_coverage * 100.0), 2),
                "unallocated_area_acres": round(float(_residual / 4047.0), 2),
            })
            park["park_summary"].update({
                "terrain_buildable_area_acres": round(float(park["land_reconciliation"].get("terrain_buildable_area_acres", 0.0)), 2),
                "boundary_setback_loss_acres": round(float(park["land_reconciliation"].get("boundary_setback_loss_acres", 0.0)), 2),
                "infrastructure_corridor_loss_acres": round(float(park["land_reconciliation"].get("infrastructure_corridor_loss_acres", 0.0)), 2),
            })
            park["infrastructure_complete"] = True
            # Page-3 currently uses this legacy flag; keep it synchronized
            # until the workflow is fully migrated to one state key.
            park["infrastructure_done"] = True

            st.session_state["park_data"] = park

            st.success("✅ Park infrastructure generated & saved")

        except Exception as e:
            logger.exception("Park generation failed")
            st.error("❌ Park generation failed")
            st.exception(e)

# -------------------------------------------------
# Display results
# -------------------------------------------------
if park.get("infrastructure_complete"):

    st.markdown("---")
    st.header("📊 Park Infrastructure Summary")
    st.json(park.get("park_summary", {}))

    _spine = park.get("spine_utm")
    if _spine is not None and not _spine.is_empty:
        _spine_len_m = float(_spine.length)
        if _spine_len_m < 50.0:
            st.warning(
                f"⚠️ Spine road length is only {_spine_len_m:.1f} m. "
                "Review the spine placement before proceeding to site design."
            )

    # Geometry reconciliation — visible before Page-3 uses these sites.
    try:
        _sites_check = park.get("sites_utm")
        _usable_check = park.get("usable_area_utm")
        if _sites_check is not None and _usable_check is not None and not _sites_check.empty:
            _assigned = _sites_check.geometry.union_all().area
            _usable = _usable_check.area
            _cov = _assigned / _usable if _usable else 0.0
            _res = max(0.0, _usable - _assigned)
            st.subheader("✅ Site Geometry Reconciliation")
            vc1, vc2, vc3 = st.columns(3)
            vc1.metric("Usable Land", f"{_usable/4047:.2f} ac")
            vc2.metric("Site Coverage", f"{_cov*100:.1f}%")
            vc3.metric("Unallocated", f"{_res/4047:.2f} ac")
    except Exception as _val_err:
        st.warning(f"Geometry reconciliation unavailable: {_val_err}")

    # -------------------------------
    # Engineering land reconciliation
    # -------------------------------
    _land = park.get("land_reconciliation", {})
    if _land:
        st.subheader("📐 Engineering Land Reconciliation")
        _raw = float(_land.get("terrain_buildable_area_acres", 0.0))
        _setback = float(_land.get("boundary_setback_loss_acres", 0.0))
        _corridor = float(_land.get("infrastructure_corridor_loss_acres", 0.0))
        _usable = float(_land.get("engineering_usable_area_acres", 0.0))
        _land_df = pd.DataFrame({
            "Item": [
                "Terrain buildable area",
                "Boundary setback loss",
                "Road / 33kV / drainage corridor loss",
                "Engineering usable area",
            ],
            "Area (acres)": [round(_raw, 2), round(-_setback, 2), round(-_corridor, 2), round(_usable, 2)],
        })
        st.dataframe(_land_df, hide_index=True, width="stretch")

    # -------------------------------
    # Sites table
    # -------------------------------
    st.subheader("🧩 Sites Generated")

    sites = park.get("sites")

    if sites is None or sites.empty:
        st.warning("No sites generated.")
    else:
        st.dataframe(
            sites[["site_id", "area_acres", "ac_mw", "dc_mw"]],
            width="stretch"
        )

        # -------------------------------
        # Site KMZ downloads
        # -------------------------------
        st.subheader("⬇️ Site KMZ Exports")

        for _, row in sites.iterrows():
            site_id = row["site_id"]

            kmz = export_site_kmz(row, sites)

            st.download_button(
                label=f"Download {site_id}",
                data=kmz.getvalue(),
                file_name=f"{site_id}.kmz",
                mime="application/vnd.google-earth.kmz",
                key=f"kmz_{site_id}"
            )

    # -------------------------------------------------
    # Manual internal-area deletion (within a site)
    # -------------------------------------------------
    st.subheader("✂️ Remove Area Within a Site")
    st.caption(
        "Select a site, then draw a rectangle or polygon over the portion of that site "
        "you want to remove. The parent SITE ID remains; only the selected land area is cut out."
    )

    _edit_site_ids = [str(x) for x in sites["site_id"].tolist()] if sites is not None and not sites.empty else []
    if _edit_site_ids:
        _edit_site_id = st.selectbox(
            "Site to edit",
            options=_edit_site_ids,
            key="page2_internal_edit_site",
            help="Choose the existing site from which you want to remove a portion of land."
        )
        st.info(
            "On the Skeleton View below, use the drawing toolbar to draw a **rectangle or polygon** "
            "around the area to be removed. Only the part that overlaps the selected site will be removed."
        )
        st.session_state["page2_internal_edit_site_id"] = _edit_site_id

    # ── Switchyard & Gate relocation ─────────────────────────
    st.markdown("---")
    st.subheader("📍 Relocate Switchyard / Main Gate")
    st.caption(
        "Override the auto-placed positions if needed. "
        "Enter new coordinates in WGS84 (lat/lon) — the map will update."
    )

    _fac     = park.get("facilities", {}) or {}
    _sw      = _fac.get("switchyard")
    _gt      = _fac.get("main_gate")
    # facilities are stored in WGS84 (converted in park_generator before saving)
    # so .x = longitude, .y = latitude — read directly, no conversion needed
    _bnd_wgs84 = park.get("boundary")
    _fallback_lat = float(_bnd_wgs84.centroid.y) if _bnd_wgs84 else 13.0
    _fallback_lon = float(_bnd_wgs84.centroid.x) if _bnd_wgs84 else 76.0

    _sw_lat = float(_sw.y) if _sw else _fallback_lat
    _sw_lon = float(_sw.x) if _sw else _fallback_lon
    _gt_lat = float(_gt.y) if _gt else _fallback_lat
    _gt_lon = float(_gt.x) if _gt else _fallback_lon

    _rc1, _rc2 = st.columns(2)
    with _rc1:
        st.markdown("**⚡ Switchyard**")
        new_sw_lat = st.number_input("Switchyard Latitude",  value=round(_sw_lat, 6), format="%.6f", key="sw_lat")
        new_sw_lon = st.number_input("Switchyard Longitude", value=round(_sw_lon, 6), format="%.6f", key="sw_lon")

    with _rc2:
        st.markdown("**🚧 Main Gate**")
        new_gt_lat = st.number_input("Gate Latitude",  value=round(_gt_lat, 6), format="%.6f", key="gt_lat")
        new_gt_lon = st.number_input("Gate Longitude", value=round(_gt_lon, 6), format="%.6f", key="gt_lon")

    if st.button("📍 Apply Relocations", key="apply_reloc"):
        try:
            # facilities are stored as WGS84 Points — store directly
            park["facilities"]["switchyard"] = Point(new_sw_lon, new_sw_lat)
            park["facilities"]["main_gate"]  = Point(new_gt_lon, new_gt_lat)
            st.success(
                f"✅ Switchyard → ({new_sw_lat:.6f}, {new_sw_lon:.6f}) | "
                f"Gate → ({new_gt_lat:.6f}, {new_gt_lon:.6f})"
            )
            st.rerun()
        except Exception as _re:
            st.error(f"Relocation failed: {_re}")

    # -------------------------------
    # Skeleton Map
    # -------------------------------
    # -------------------------------
    # Skeleton Map
    # -------------------------------
    st.markdown("---")
    st.header("🗺️ Park Infrastructure — Skeleton View")

    # Stage 4.4: Page-1 grading and post-grading D8 results are carried in the
    # shared park_data object. They are optional overlays; they do not replace
    # Page-2 generated roads, parcels, or its existing conceptual drainage.
    _p2_fgl = park.get("drainage_grading_surface")
    _p2_fgl_summary = park.get("drainage_grading_summary") or {}
    _p2_post_flow = park.get("drainage_post_grading_flow_grid")
    _p2_terminals = park.get("drainage_post_grading_flow_terminals")
    _p2_flow_meta = park.get("drainage_flow_surface_meta") or {}
    _p2_flow_resolution = park.get("drainage_flow_resolution_m")
    _p2_has_grading = isinstance(_p2_fgl, pd.DataFrame) and not _p2_fgl.empty
    _p2_has_flow = isinstance(_p2_post_flow, pd.DataFrame) and not _p2_post_flow.empty

    st.subheader("⛰️ Page-1 Grading & Drainage Design")
    if _p2_has_grading or _p2_has_flow:
        _p2_source = str(_p2_flow_meta.get("elevation_source", "unknown")).upper()
        _p2_mode = _p2_fgl_summary.get("grading_mode", park.get("drainage_grading_mode", "not specified"))
        _p2_grid_text = f"{float(_p2_flow_resolution):g} m" if _p2_flow_resolution is not None else "not recorded"
        _p2_m1, _p2_m2, _p2_m3 = st.columns(3)
        _p2_m1.metric("Elevation source", _p2_source)
        _p2_m2.metric("Analysis grid", _p2_grid_text)
        _p2_m3.metric("Grading strategy", str(_p2_mode))
        if _p2_source not in {"SURVEY", "SURVEY XYZ"}:
            st.warning("Page-1 grading/drainage overlay is based on DEM terrain and is preliminary. Verify levels and outfalls before design acceptance.")
        st.caption("Use the map layer control to show or hide the proposed FGL cut/fill cells, post-grading drainage corridors and terminal candidates.")
        _p2_flow_summary = park.get("drainage_post_grading_flow_summary") or {}
        if _p2_has_grading:
            _p2_s1, _p2_s2, _p2_s3, _p2_s4 = st.columns(4)
            _p2_s1.metric("Proposed cut", f"{float(_p2_fgl_summary.get('cut_volume_m3', 0.0)):,.0f} m³")
            _p2_s2.metric("Proposed fill", f"{float(_p2_fgl_summary.get('fill_volume_m3', 0.0)):,.0f} m³")
            _p2_s3.metric("Post-grade D8 links", f"{int(_p2_flow_summary.get('network_link_count', 0)):,}")
            _p2_s4.metric("Terminal candidates", f"{int(_p2_flow_summary.get('terminal_candidates', 0)):,}")
    else:
        st.info("No proposed finished-ground surface or post-grading drainage network is stored yet. Run Terrain Analysis and generate a grading surface on Page 1 to make these overlays available here.")

    try:
        m = folium.Map(
            location=[boundary.centroid.y, boundary.centroid.x],
            zoom_start=15,
            control_scale=True
        )

        # -------------------------------------------------
        # PARK BOUNDARY
        # -------------------------------------------------
        folium.Polygon(
            locations=[(lat, lon) for lon, lat in boundary.exterior.coords],
            color="black",
            weight=2,
            fill=False,
            tooltip="Park Boundary"
        ).add_to(m)

        # -------------------------------------------------
        # STAGE 4.4 – PAGE-1 PROPOSED FINISHED GROUND OVERLAY
        # Keep this layer beneath parcel fills so site labels and boundaries
        # remain legible. Each grid cell is clipped to the KMZ polygon.
        # -------------------------------------------------
        if _p2_has_grading:
            try:
                _p2_boundary_utm = gpd.GeoSeries([boundary], crs="EPSG:4326").to_crs(epsg=int(utm_epsg)).iloc[0]
                _p2_grading_gdf = build_grading_overlay_gdf(
                    _p2_fgl,
                    boundary_utm=_p2_boundary_utm,
                    grid_resolution_m=float(_p2_flow_resolution or park.get("grid_size", 50.0)),
                    source_epsg=int(utm_epsg),
                )
                if not _p2_grading_gdf.empty:
                    _p2_grade_layer = folium.FeatureGroup(name="Page-1 Proposed FGL – Cut / Fill", show=False)
                    folium.GeoJson(
                        data=json.loads(_p2_grading_gdf.to_json()),
                        name="Proposed FGL cut-fill cells",
                        style_function=lambda feature: {
                            "color": "#a50f15" if feature["properties"].get("kind") == "CUT" else ("#08519c" if feature["properties"].get("kind") == "FILL" else "#777777"),
                            "weight": 0.25,
                            "fillColor": "#ef3b2c" if feature["properties"].get("kind") == "CUT" else ("#6baed6" if feature["properties"].get("kind") == "FILL" else "#bdbdbd"),
                            "fillOpacity": 0.42,
                        },
                        tooltip=folium.GeoJsonTooltip(fields=["tooltip"], aliases=[""], sticky=False),
                    ).add_to(_p2_grade_layer)
                    _p2_grade_layer.add_to(m)
            except Exception as _p2_grade_map_exc:
                st.warning(f"Proposed FGL overlay could not be drawn: {_p2_grade_map_exc}")

        # -------------------------------------------------
        # SITES
        # -------------------------------------------------
        for _, r in park["sites"].iterrows():
            geom = r.geometry
            if geom is None or geom.is_empty:
                continue
            # Flatten MultiPolygon → individual polygons
            parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
            for part in parts:
                folium.Polygon(
                    locations=[(lat, lon) for lon, lat in part.exterior.coords],
                    color="green",
                    weight=1,
                    fill=True,
                    fill_opacity=0.3,
                    tooltip=f"{r['site_id']} | {r['ac_mw']} MW | Click to inspect",
                    popup=folium.Popup(
                        f"<b>{r['site_id']}</b><br>AC Capacity: {float(r['ac_mw']):.2f} MW<br>Area: {float(r['area_acres']):.2f} acres<br><i>Use the 'Remove Area Within a Site' control above to remove part of this site.</i>",
                        max_width=300
                    )
                ).add_to(m)

            # Keep the site ID visible on the map so it is clear which green
            # region is being edited.
            try:
                _site_label_pt = geom.representative_point()
                folium.Marker(
                    location=[_site_label_pt.y, _site_label_pt.x],
                    icon=folium.DivIcon(html=(
                        '<div style="'
                        'font-family:Arial,sans-serif;'
                        'font-size:13px;'
                        'line-height:1.25;'
                        'font-weight:600;'
                        'color:#0b5d1e;'
                        'text-align:center;'
                        'white-space:nowrap;'
                        'background:transparent;'
                        'border:none;'
                        'padding:0;'
                        'margin:0;'
                        'text-shadow:0 1px 2px rgba(255,255,255,0.95);'
                        'transform:translate(-50%,-50%);'
                        '">'
                        f'<div style="font-size:15px;font-weight:700;">{r["site_id"]}</div>'
                        f'<div>{float(r["ac_mw"]):.2f} MW</div>'
                        f'<div>{float(r["area_acres"]):.2f} acres</div>'
                        '</div>'
                    )),
                    tooltip=f'{r["site_id"]} | {float(r["ac_mw"]):.2f} MW | {float(r["area_acres"]):.2f} acres',
                ).add_to(m)
            except Exception:
                pass

        # -------------------------------------------------
        # ROADS (DICT OF GEOSERIES)
        # -------------------------------------------------
        roads = park.get("roads", {})
        if roads is None:
            roads = {}
        
        # Main access road (orange)
        if "main_access" in roads and roads["main_access"] is not None:
            for geom in roads["main_access"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#FF8C00",
                    weight=4,
                    tooltip="Main Access Road"
                ).add_to(m)
        
        # Spine road (dark gray)
        if "spine" in roads and roads["spine"] is not None:
            for geom in roads["spine"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#444444",
                    weight=3,
                    tooltip="Spine Road"
                ).add_to(m)
        
        # Site spurs (gray)
        if "site_spurs" in roads and roads["site_spurs"] is not None:
            for geom in roads["site_spurs"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#888888",
                    weight=2,
                    tooltip="Site Spur"
                ).add_to(m)

        # -------------------------------------------------
        # DRAINAGE CHANNELS
        # When Page-1 D8 data is available, Page-2's infrastructure drainage
        # channels are generated from those actual downstream corridors.
        # Otherwise retain the original road-spur conceptual drain fallback.
        # -------------------------------------------------
        drainage = park.get("drainage", {}) or {}
        _p2_integrated_drainage = drainage.get("source") == "page1_post_grading_d8"
        if _p2_integrated_drainage:
            _main_drain_layer = folium.FeatureGroup(name="Page-2 Main Drainage Channels", show=True)
            _secondary_drain_layer = folium.FeatureGroup(name="Page-2 Secondary Drainage Channels", show=True)
            for _geom in drainage.get("main_channels", []):
                try:
                    folium.PolyLine(
                        locations=[(float(lat), float(lon)) for lon, lat in _geom.coords],
                        color="#b2182b", weight=4, opacity=0.95,
                        tooltip="Indicative main drainage channel – derived from Page-1 post-grading D8",
                    ).add_to(_main_drain_layer)
                except Exception:
                    continue
            for _geom in drainage.get("secondary_channels", []):
                try:
                    folium.PolyLine(
                        locations=[(float(lat), float(lon)) for lon, lat in _geom.coords],
                        color="#f07c00", weight=2, opacity=0.9,
                        tooltip="Indicative secondary drainage channel – derived from Page-1 post-grading D8",
                    ).add_to(_secondary_drain_layer)
                except Exception:
                    continue
            _main_drain_layer.add_to(m)
            _secondary_drain_layer.add_to(m)
        elif drainage.get("channels") is not None:
            _legacy_drain_layer = folium.FeatureGroup(name="Conceptual Drainage Channels (legacy fallback)", show=True)
            for geom in drainage.get("channels", []):
                try:
                    folium.PolyLine(
                        locations=[(float(lat), float(lon)) for lon, lat in geom.coords],
                        color="#0066CC", weight=2, dash_array="5,5",
                        tooltip="Conceptual drainage channel – road-spur fallback",
                    ).add_to(_legacy_drain_layer)
                except Exception:
                    continue
            _legacy_drain_layer.add_to(m)

        if _p2_integrated_drainage:
            st.caption(
                f"Page-2 drainage channels are derived from Page-1 post-grading D8 corridors "
                f"({len(drainage.get('main_channels', []))} main and "
                f"{len(drainage.get('secondary_channels', []))} secondary line features). "
                "These are indicative alignments, not hydraulically sized channels or approved outfalls."
            )

        # -------------------------------------------------
        # STAGE 4.4 – PAGE-1 POST-GRADING D8 NETWORK OVERLAY
        # Keep the analytical link overlay only when it has not already been
        # consumed as Page-2 infrastructure channels, avoiding duplicate lines.
        # -------------------------------------------------
        if _p2_has_flow and not _p2_integrated_drainage:
            try:
                _p2_flow_gdf = build_drainage_overlay_gdf(_p2_post_flow, source_epsg=int(utm_epsg))
                if not _p2_flow_gdf.empty:
                    _p2_flow_layer = folium.FeatureGroup(name="Page-1 Post-Grading D8 Corridors", show=True)
                    folium.GeoJson(
                        data=json.loads(_p2_flow_gdf.to_json()),
                        name="Post-grading D8 links",
                        style_function=lambda feature: {
                            "color": "#b2182b" if feature["properties"].get("kind") == "MAIN" else "#f07c00",
                            "weight": 4 if feature["properties"].get("kind") == "MAIN" else 2,
                            "opacity": 0.9,
                        },
                        tooltip=folium.GeoJsonTooltip(fields=["tooltip"], aliases=[""], sticky=False),
                    ).add_to(_p2_flow_layer)
                    _p2_flow_layer.add_to(m)
            except Exception as _p2_flow_map_exc:
                st.warning(f"Post-grading drainage overlay could not be drawn: {_p2_flow_map_exc}")

        if isinstance(_p2_terminals, pd.DataFrame) and not _p2_terminals.empty:
            try:
                _p2_terminal_gdf = build_terminal_overlay_gdf(_p2_terminals, source_epsg=int(utm_epsg))
                if not _p2_terminal_gdf.empty:
                    _p2_terminal_layer = folium.FeatureGroup(name="Page-1 Drainage Terminals / Outfall Candidates", show=True)
                    for _p2_term in _p2_terminal_gdf.itertuples(index=False):
                        _is_boundary = str(_p2_term.terminal_type).lower() == "boundary_edge_candidate"
                        # build_terminal_overlay_gdf reprojects terminal Points to
                        # EPSG:4326. GeoDataFrame rows expose the Shapely geometry,
                        # not latitude/longitude attributes; for Point geometry,
                        # x is longitude and y is latitude.
                        _term_lat = float(_p2_term.geometry.y)
                        _term_lon = float(_p2_term.geometry.x)
                        folium.CircleMarker(
                            location=(_term_lat, _term_lon),
                            radius=5,
                            color="#2166ac" if _is_boundary else "#762a83",
                            fill=True,
                            fill_color="#4393c3" if _is_boundary else "#af8dc3",
                            fill_opacity=0.9,
                            tooltip=str(_p2_term.tooltip),
                            popup=folium.Popup(
                                f"<b>{_p2_term.terminal_id}</b><br>{_p2_term.network_id}<br>"
                                f"{'Boundary outfall candidate – verify receiving drain' if _is_boundary else 'Interior sink / coverage stop – investigate'}<br>"
                                f"Elevation: {float(_p2_term.elevation_m):.2f} m",
                                max_width=320,
                            ),
                        ).add_to(_p2_terminal_layer)
                    _p2_terminal_layer.add_to(m)
            except Exception as _p2_terminal_exc:
                st.warning(f"Drainage terminal overlay could not be drawn: {_p2_terminal_exc}")

        # STAGE 4.4.4 – GENERIC SPINE-ALIGNED MASTER COLLECTOR
        # Stored separately from the Page-1 D8 lines and existing infrastructure
        # drainage. This is an optional proposed drainage-design overlay.
        _master_drainage = park.get("drainage_master_design") or {}
        if _master_drainage.get("main_collector_lines_wgs84"):
            try:
                _master_layer = folium.FeatureGroup(name="Proposed Main Collector to External Outfall", show=True)
                for _line in _master_drainage.get("main_collector_lines_wgs84", []):
                    folium.PolyLine(
                        locations=[(float(lat), float(lon)) for lon, lat in _line.coords],
                        color="#08306b", weight=6, opacity=0.95,
                        tooltip="Proposed main collector – spine-aligned; verify invert profile",
                    ).add_to(_master_layer)
                for _line in _master_drainage.get("branch_connectors_wgs84", []):
                    folium.PolyLine(
                        locations=[(float(lat), float(lon)) for lon, lat in _line.coords],
                        color="#238b8e", weight=3, opacity=0.95, dash_array="7,5",
                        tooltip="Proposed tributary-to-main-collector connection – level check required",
                    ).add_to(_master_layer)
                _boundary_crossing_wgs = _master_drainage.get("boundary_crossing_point_wgs84")
                if _boundary_crossing_wgs is not None:
                    folium.CircleMarker(
                        location=(float(_boundary_crossing_wgs.y), float(_boundary_crossing_wgs.x)),
                        radius=6, color="#fdae61", fill=True, fill_color="#fdae61",
                        tooltip="Proposed park-boundary drainage crossing",
                    ).add_to(_master_layer)
                _external_outfall_wgs = _master_drainage.get("outfall_point_wgs84")
                if _external_outfall_wgs is not None:
                    folium.Marker(
                        location=(float(_external_outfall_wgs.y), float(_external_outfall_wgs.x)),
                        icon=folium.Icon(color="blue", icon="tint", prefix="fa"),
                        tooltip=f"Proposed external outfall candidate – {_master_drainage.get('outfall_reference', 'verify')}",
                        popup=folium.Popup(
                            "<b>Proposed external outfall candidate</b><br>Not a verified receiving channel or approved discharge point.",
                            max_width=320,
                        ),
                    ).add_to(_master_layer)
                _master_layer.add_to(m)
            except Exception as _master_map_exc:
                st.warning(f"Main collector overlay could not be drawn: {_master_map_exc}")

        # -------------------------------------------------
        # ELECTRICAL
        # -------------------------------------------------
        electrical = park.get("electrical", {})
        if electrical is None:
            electrical = {}
        
        # Main 33kV OH line (dark orange/brown - thick)
        if "oh_line_33kv" in electrical and electrical["oh_line_33kv"] is not None:
            for geom in electrical["oh_line_33kv"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#CC5500",
                    weight=3,
                    tooltip="33kV OH Line (Main Spine)"
                ).add_to(m)
        
        # Droppers to sites (red, thinner)
        if "droppers" in electrical and electrical["droppers"] is not None:
            for geom in electrical["droppers"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="red",
                    weight=2,
                    dash_array="3,3",
                    tooltip="Dropper to Site Pole Yard"
                ).add_to(m)
        
        # Evacuation line (dark red, thick)
        if "evacuation_line" in electrical and electrical["evacuation_line"] is not None:
            for geom in electrical["evacuation_line"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#8B0000",
                    weight=4,
                    tooltip="33kV Evacuation Line to Grid"
                ).add_to(m)
        
        # Poles (small orange circles)
        if "poles" in electrical and electrical["poles"] is not None:
            for geom in electrical["poles"]:
                folium.CircleMarker(
                    location=(geom.y, geom.x),
                    radius=2,
                    color="#CC5500",
                    fill=True,
                    fill_opacity=0.8,
                    tooltip="33kV Pole"
                ).add_to(m)
        
        # Site pole yards (yellow squares - 4-pole substations)
        if "site_pole_yards" in electrical and electrical["site_pole_yards"] is not None:
            for geom in electrical["site_pole_yards"]:
                folium.RegularPolygonMarker(
                    location=(geom.y, geom.x),
                    number_of_sides=4,
                    radius=6,
                    color="orange",
                    fill=True,
                    fill_color="yellow",
                    fill_opacity=0.9,
                    tooltip="Site Pole Yard (4-Pole Substation)"
                ).add_to(m)

        # -------------------------------------------------
        # FACILITIES
        # -------------------------------------------------
        facilities = park.get("facilities", {})
        if facilities is None:
            facilities = {}
        
        # Main gate (LARGER, more visible)
        if "main_gate" in facilities and facilities["main_gate"] is not None:
            geom = facilities["main_gate"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>Main Gate</b><br>Park Entry Point",
                icon=folium.Icon(color="orange", icon="road", prefix="fa", icon_color="white"),
                tooltip="🚧 Main Gate"
            ).add_to(m)
            # Add circle around gate for visibility
            folium.CircleMarker(
                location=(geom.y, geom.x),
                radius=8,
                color="orange",
                fill=True,
                fill_color="orange",
                fill_opacity=0.3,
                weight=2
            ).add_to(m)
        
        # Switchyard (LARGER, more visible)
        if "switchyard" in facilities and facilities["switchyard"] is not None:
            geom = facilities["switchyard"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>Switchyard</b><br>33kV Pooling Station",
                icon=folium.Icon(color="purple", icon="bolt", prefix="fa", icon_color="white"),
                tooltip="⚡ Switchyard"
            ).add_to(m)
            # Add square around switchyard
            folium.CircleMarker(
                location=(geom.y, geom.x),
                radius=10,
                color="purple",
                fill=True,
                fill_color="purple",
                fill_opacity=0.3,
                weight=3
            ).add_to(m)
        
        # O&M Building
        if "om_building" in facilities and facilities["om_building"] is not None:
            geom = facilities["om_building"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>O&M Building</b><br>Operations & Maintenance",
                icon=folium.Icon(color="blue", icon="building", prefix="fa", icon_color="white"),
                tooltip="🏢 O&M Building"
            ).add_to(m)

        # Fit the map to the actual park boundary instead of using a fixed
        # zoom level. This keeps small and large parks equally readable.
        try:
            _min_lon, _min_lat, _max_lon, _max_lat = boundary.bounds
            # Include the proposed external outfall marker in the view bounds
            # so the complete discharge reach remains visible on the map.
            _fit_geoms = [boundary]
            _fit_master = park.get("drainage_master_design") or {}
            _fit_outfall = _fit_master.get("outfall_point_wgs84")
            if _fit_outfall is not None and not _fit_outfall.is_empty:
                _fit_geoms.append(_fit_outfall)
            _fit_bounds_geom = gpd.GeoSeries(_fit_geoms, crs="EPSG:4326").union_all()
            _min_lon, _min_lat, _max_lon, _max_lat = _fit_bounds_geom.bounds
            m.fit_bounds([[_min_lat, _min_lon], [_max_lat, _max_lon]], padding=(20, 20))
        except Exception:
            pass

        # Add layer control
        folium.LayerControl(collapsed=False).add_to(m)

        # Drawing tool for removing a portion of a selected site.
        # Only rectangle/polygon are enabled; this does not alter the park boundary.
        Draw(
            export=False,
            position="topleft",
            draw_options={
                "polyline": False,
                "polygon": True,
                "rectangle": True,
                "circle": False,
                "circlemarker": False,
                "marker": False,
            },
            edit_options={"edit": False, "remove": False},
        ).add_to(m)

        _map_result = st_folium(
            m,
            height=600,
            width=1400,
            returned_objects=["all_drawings"],
            key="page2_skeleton_map",
        )


    except Exception as e:
        st.error("Map rendering failed")
        st.exception(e)
        import traceback
        st.code(traceback.format_exc())

    # -------------------------------------------------
    # STAGE 4.4.3 – DRAINAGE DASHBOARD
    # Read-only dashboard: explains the D8 route-to-terminal picture and
    # identifies geometric road/drain intersection candidates. It does not
    # change roads, site geometry, drainage channels or culvert inventory.
    # -------------------------------------------------
    st.markdown("---")
    st.header("💧 Drainage Dashboard – How Water Drains Through the Park")
    _dash_drainage = park.get("drainage", {}) or {}
    _dash_flow = park.get("drainage_post_grading_flow_grid")
    _dash_terminals = park.get("drainage_post_grading_flow_terminals")
    _dash_resolution = float(park.get("drainage_flow_resolution_m") or 0.0)
    _dash_summary = park.get("drainage_post_grading_flow_summary") or {}
    _dash_source = str((park.get("drainage_flow_surface_meta") or {}).get("elevation_source", "unknown")).upper()

    if (
        _dash_drainage.get("source") == "page1_post_grading_d8"
        and isinstance(_dash_flow, pd.DataFrame) and not _dash_flow.empty
        and _dash_resolution > 0
    ):
        st.caption(
            "Route tracing uses the Page-1 post-grading D8 receiver links. "
            "Boundary markers are outfall candidates only; they are not verified receiving drains. "
            "The dashboard is a planning-level routing view, not hydraulic channel sizing."
        )
        _rain_col, _runoff_col, _basis_col = st.columns([1, 1, 2])
        with _rain_col:
            _dash_rain = st.number_input(
                "Screening rainfall intensity (mm/hour)", min_value=1.0, max_value=300.0,
                value=75.0, step=5.0,
                key="p2_drain_dashboard_rain",
                help="Editable screening assumption only. Use a site-specific design intensity for engineering design.",
            )
        with _runoff_col:
            _dash_c = st.number_input(
                "Composite runoff coefficient", min_value=0.05, max_value=1.0,
                value=0.60, step=0.05,
                key="p2_drain_dashboard_c",
                help="Editable preliminary coefficient. Refine by land cover and infiltration characteristics.",
            )
        with _basis_col:
            st.info(
                f"Elevation source: **{_dash_source}** | Grid: **{_dash_resolution:g} m** | "
                f"Accumulation threshold: **{int(_dash_summary.get('accumulation_threshold_cells', 0))} cells**. "
                "Peak-flow values below are Rational-Method screening estimates from grid-derived accumulated area; "
                "they are not verified design discharges."
            )

        try:
            _dash_payload = build_drainage_dashboard_payload(
                flow_grid=_dash_flow,
                terminals=_dash_terminals if isinstance(_dash_terminals, pd.DataFrame) else None,
                drainage=_dash_drainage,
                roads=park.get("roads", {}) or {},
                sites=park.get("sites"),
                grid_resolution_m=_dash_resolution,
                source_epsg=int(utm_epsg),
                rainfall_intensity_mm_hr=float(_dash_rain),
                runoff_coefficient=float(_dash_c),
            )
            _dash_route_df = _dash_payload["route_register"]
            _dash_terminal_df = _dash_payload["terminal_register"]
            _dash_crossing_df = _dash_payload["crossing_register"]
            _dash_site_df = _dash_payload["site_register"]
            # Once a master collector exists, include its trunk and tributary
            # connectors in the road-crossing candidate register as well.
            _existing_master_for_crossings = park.get("drainage_master_design") or {}
            if _existing_master_for_crossings:
                try:
                    _master_crossing_drainage = {
                        "main_channels": list(gpd.GeoSeries(
                            _existing_master_for_crossings.get("main_collector_lines_utm", []),
                            crs=f"EPSG:{utm_epsg}",
                        ).to_crs(epsg=4326)),
                        "secondary_channels": list(gpd.GeoSeries(
                            _existing_master_for_crossings.get("branch_connectors_utm", []),
                            crs=f"EPSG:{utm_epsg}",
                        ).to_crs(epsg=4326)) if _existing_master_for_crossings.get("branch_connectors_utm") else [],
                    }
                    _master_crossings = build_road_crossing_register(
                        _master_crossing_drainage, park.get("roads", {}) or {}, source_epsg=int(utm_epsg)
                    )
                    if not _master_crossings.empty:
                        _master_crossings["drain_type"] = _master_crossings["drain_type"].map(
                            {"Main": "Main collector", "Secondary": "Tributary connector"}
                        ).fillna(_master_crossings["drain_type"])
                    _dash_crossing_df = merge_road_crossing_registers(
                        _dash_crossing_df, _master_crossings, dedupe_tolerance_m=2.0
                    )
                except Exception as _master_crossing_exc:
                    logger.warning("Could not add master collector road crossings: %s", _master_crossing_exc)

            # -------------------------------------------------
            # STAGE 4.4.4 – GENERIC MAIN COLLECTOR / OUTFALL DESIGN
            # The spine road is the preferred trunk corridor. Tributary lines
            # connect D8 network terminals to that trunk, subject to editable
            # geometric connection limits and later invert-level verification.
            # -------------------------------------------------
            st.markdown("---")
            st.subheader("🚰 Main Collector & External Outfall Design")
            st.caption(
                "The main collector is placed parallel and close to the spine road wherever the KMZ boundary permits. "
                "D8 terminal branches are connected to it as proposed links. Gravity, invert levels, receiving-water levels "
                "and discharge permissions must be checked before construction."
            )
            _collector_boundary_utm = gpd.GeoSeries([park["boundary"]], crs="EPSG:4326").to_crs(epsg=int(utm_epsg)).iloc[0]
            _candidate_register = build_outfall_candidate_register(
                _dash_terminals if isinstance(_dash_terminals, pd.DataFrame) else None,
                _collector_boundary_utm,
                grid_resolution_m=_dash_resolution,
            )
            _kmz_receiving_features_all = kmz_receiving_feature_options(park, st.session_state.get("kmz_bytes"))
            # Only KMZ receiving markers outside the project boundary can be
            # offered as external outfalls. Interior river points are informative
            # features, but not an external discharge destination by themselves.
            _kmz_receiving_features = []
            _kmz_interior_feature_count = 0
            for _feature in _kmz_receiving_features_all:
                _fx, _fy = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True).transform(
                    float(_feature["longitude"]), float(_feature["latitude"])
                )
                if _collector_boundary_utm.contains(Point(float(_fx), float(_fy))):
                    _kmz_interior_feature_count += 1
                else:
                    _kmz_receiving_features.append(_feature)
            _mode_labels = ["System-suggested boundary candidate", "User-selected external point"]
            if _kmz_receiving_features:
                _mode_labels.insert(1, "KMZ receiving feature")
            _dc1, _dc2, _dc3 = st.columns([1.4, 1, 1])
            with _dc1:
                _outfall_mode_label = st.selectbox(
                    "External outfall source", _mode_labels, index=0,
                    key="p2_main_collector_outfall_mode",
                    help="The system can suggest a boundary terminal, use a user-entered outside point, or use a receiving feature already parsed from the KMZ.",
                )
            with _dc2:
                _spine_side_label = st.selectbox(
                    "Collector side of spine", ["Auto – closest to D8 branches", "Left of spine", "Right of spine"],
                    index=0, key="p2_main_collector_side",
                )
            with _dc3:
                _collector_offset = st.number_input(
                    "Offset from spine road (m)", min_value=0.0, max_value=30.0,
                    value=4.0, step=1.0, key="p2_main_collector_offset",
                    help="A small offset keeps the drain beside, rather than on top of, the road centre-line.",
                )

            if _kmz_interior_feature_count:
                st.caption(f"{_kmz_interior_feature_count} named KMZ water/drain point(s) are inside the project boundary and are not offered as external outfalls.")
            _suggested = None
            if not _candidate_register.empty:
                _candidate_sorted = _candidate_register.copy()
                _candidate_sorted["elevation_m"] = pd.to_numeric(_candidate_sorted["elevation_m"], errors="coerce")
                _candidate_sorted = _candidate_sorted.sort_values(["elevation_m", "distance_to_boundary_m"], na_position="last")
                try:
                    _suggested = suggest_outfall_point(_candidate_sorted.head(1),
                        gpd.GeoSeries([park["boundary"]], crs="EPSG:4326").to_crs(epsg=int(utm_epsg)).iloc[0], 100.0)
                except Exception:
                    _suggested = None

            _user_outfall_utm = None
            _selected_candidate_id = None
            _kmz_outfall_feature = None
            if _outfall_mode_label == "System-suggested boundary candidate":
                if _candidate_register.empty:
                    st.warning("No D8 boundary-edge candidate is available for automatic suggestion. Select a user-specified external point instead.")
                else:
                    _candidate_labels = {
                        str(r.candidate_id): (
                            f"{r.candidate_id} | {r.network_id} | "
                            f"{float(r.elevation_m):.2f} m RL" if pd.notna(r.elevation_m)
                            else f"{r.candidate_id} | {r.network_id} | elevation unavailable"
                        )
                        for r in _candidate_register.itertuples(index=False)
                    }
                    _candidate_ids = list(_candidate_labels)
                    _candidate_default = str(_candidate_register.sort_values("elevation_m", na_position="last").iloc[0].candidate_id)
                    _selected_candidate_id = st.selectbox(
                        "Suggested boundary terminal", _candidate_ids,
                        index=_candidate_ids.index(_candidate_default),
                        format_func=lambda value: _candidate_labels.get(str(value), str(value)),
                        key="p2_main_collector_candidate",
                    )
                    if _suggested:
                        st.info(
                            f"System recommendation: {_suggested['candidate_id']} ({_suggested['network_id']}); "
                            "the external point is projected beyond the boundary for planning only. Verify an actual receiving drain/watercourse."
                        )
            elif _outfall_mode_label == "KMZ receiving feature":
                _feature_labels = [f"{x['name']} ({x['latitude']:.6f}, {x['longitude']:.6f})" for x in _kmz_receiving_features]
                _feature_choice = st.selectbox("KMZ receiving feature", range(len(_kmz_receiving_features)), format_func=lambda i: _feature_labels[i], key="p2_main_collector_kmz_feature")
                _kmz_outfall_feature = _kmz_receiving_features[int(_feature_choice)]
                _tf = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
                _kx, _ky = _tf.transform(_kmz_outfall_feature["longitude"], _kmz_outfall_feature["latitude"])
                _user_outfall_utm = Point(float(_kx), float(_ky))
                st.caption("Selected KMZ feature is treated as a candidate only; confirm that it represents an authorised receiving point.")
            else:
                _default_lat = float(_suggested["outfall_point_utm"].y) if _suggested else float(park["boundary"].centroid.y)
                _default_lon = float(_suggested["outfall_point_utm"].x) if _suggested else float(park["boundary"].centroid.x)
                if _suggested:
                    _to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
                    _default_lon, _default_lat = _to_wgs.transform(_default_lon, _default_lat)
                    st.caption("The initial coordinates are a system-suggested starting point. Confirm or replace them with a real external receiving location.")
                else:
                    st.caption("Enter a point outside the KMZ project boundary. The app rejects points that fall inside the park.")
                _ul1, _ul2 = st.columns(2)
                with _ul1:
                    _user_outfall_lat = st.number_input("External outfall latitude (WGS84)", value=round(float(_default_lat), 6), format="%.6f", key="p2_main_collector_outfall_lat")
                with _ul2:
                    _user_outfall_lon = st.number_input("External outfall longitude (WGS84)", value=round(float(_default_lon), 6), format="%.6f", key="p2_main_collector_outfall_lon")
                _tf = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
                _ux, _uy = _tf.transform(float(_user_outfall_lon), float(_user_outfall_lat))
                _user_outfall_utm = Point(float(_ux), float(_uy))

            _dc4, _dc5, _dc6 = st.columns(3)
            with _dc4:
                _max_branch_connection = st.select_slider(
                    "Maximum terminal-to-collector connection (m)", options=[50, 100, 150, 200, 300, 500], value=300,
                    key="p2_main_collector_connection_limit",
                )
            with _dc5:
                _external_extension = st.select_slider(
                    "System-suggested external reach (m)", options=[25, 50, 100, 150, 200, 300], value=100,
                    key="p2_main_collector_external_extension",
                )
            with _dc6:
                _minimum_collector_slope = st.number_input(
                    "Minimum slope for level screening (%)", min_value=0.05, max_value=2.0,
                    value=0.20, step=0.05, key="p2_main_collector_screen_slope",
                    help="Used only if a receiving invert/water level is supplied. This does not design the drain invert profile.",
                )
            _has_receiving_invert = st.checkbox(
                "I have a receiving invert / water level", value=False,
                key="p2_main_collector_has_receiving_invert",
                help="Only enable this when a receiving level is known from survey or a reliable source.",
            )
            _receiving_invert = None
            if _has_receiving_invert:
                _receiving_invert = st.number_input(
                    "Receiving invert / water level (m RL)",
                    min_value=-1000.0, max_value=10000.0, value=0.0, step=0.10,
                    key="p2_main_collector_receiving_invert",
                    help="Measured/verified receiving level. This enables only a preliminary level-head screen.",
                )

            _design_button_disabled = (
                (_outfall_mode_label == "System-suggested boundary candidate" and _candidate_register.empty)
                or (_outfall_mode_label == "User-selected external point" and _user_outfall_utm is None)
            )
            if st.button(
                "🔗 Generate spine-aligned main collector and connect D8 branches",
                key="p2_generate_spine_main_collector", type="primary", disabled=_design_button_disabled,
            ):
                try:
                    _boundary_utm_for_collector = _collector_boundary_utm
                    _drain_lines_utm = []
                    for _key in ("main_channels", "secondary_channels"):
                        _values = _dash_drainage.get(_key, [])
                        if _values is not None and len(_values) > 0:
                            _drain_lines_utm.extend(list(gpd.GeoSeries(list(_values), crs="EPSG:4326").to_crs(epsg=int(utm_epsg))))
                    _mode_value = "system_suggested" if _outfall_mode_label == "System-suggested boundary candidate" else ("kmz_feature" if _outfall_mode_label == "KMZ receiving feature" else "user_selected")
                    _design = design_spine_aligned_collector(
                        spine_utm=park.get("spine_utm"),
                        boundary_utm=_boundary_utm_for_collector,
                        terminals=_dash_terminals if isinstance(_dash_terminals, pd.DataFrame) else None,
                        drainage_lines_utm=_drain_lines_utm,
                        outfall_mode=_mode_value,
                        selected_candidate_id=_selected_candidate_id,
                        user_outfall_utm=_user_outfall_utm,
                        spine_side=("auto" if _spine_side_label.startswith("Auto") else ("left" if _spine_side_label.startswith("Left") else "right")),
                        spine_offset_m=float(_collector_offset),
                        max_branch_connection_m=float(_max_branch_connection),
                        external_extension_m=float(_external_extension),
                        minimum_screening_slope_pct=float(_minimum_collector_slope),
                        receiving_invert_m=float(_receiving_invert) if _has_receiving_invert and _receiving_invert is not None else None,
                        grid_resolution_m=float(_dash_resolution),
                    )
                    _design["main_collector_lines_wgs84"] = list(gpd.GeoSeries(_design["main_collector_lines_utm"], crs=f"EPSG:{utm_epsg}").to_crs(epsg=4326))
                    _design["branch_connectors_wgs84"] = list(gpd.GeoSeries(_design["branch_connectors_utm"], crs=f"EPSG:{utm_epsg}").to_crs(epsg=4326)) if _design["branch_connectors_utm"] else []
                    _design["outfall_point_wgs84"] = gpd.GeoSeries([_design["outfall_point_utm"]], crs=f"EPSG:{utm_epsg}").to_crs(epsg=4326).iloc[0]
                    _design["boundary_crossing_point_wgs84"] = gpd.GeoSeries([_design["boundary_crossing_point_utm"]], crs=f"EPSG:{utm_epsg}").to_crs(epsg=4326).iloc[0]
                    park["drainage_master_design"] = _design
                    st.session_state["park_data"] = park
                    st.success("Spine-aligned collector and tributary connection concept generated.")
                    st.rerun()
                except Exception as _collector_exc:
                    logger.exception("Main collector design failed")
                    st.error(f"Main collector could not be generated: {_collector_exc}")

            _master_design = park.get("drainage_master_design") or {}
            if _master_design:
                _mc1, _mc2, _mc3, _mc4 = st.columns(4)
                _mc1.metric("Main collector total", f"{_master_design.get('total_main_collector_length_m', 0.0)/1000:.2f} km")
                _mc2.metric("D8 terminals connected", f"{_master_design.get('connected_terminal_count', 0)}")
                _mc3.metric("Unconnected terminals", f"{_master_design.get('unconnected_terminal_count', 0)}")
                _mc4.metric("Collector side", str(_master_design.get("selected_spine_side", "—")).replace("_", " ").title())
                st.info(
                    f"Outfall source: **{_master_design.get('outfall_source', '—')}** | "
                    f"Reference: **{_master_design.get('outfall_reference', '—')}** | "
                    f"Gravity screen: **{_master_design.get('gravity_screen_status', 'Not checked')}**"
                )
                if not _master_design.get("connection_register", pd.DataFrame()).empty:
                    with st.expander("🔗 Tributary-to-main-collector connection register", expanded=True):
                        st.dataframe(_master_design["connection_register"], use_container_width=True, hide_index=True)
                if not _master_design.get("unconnected_register", pd.DataFrame()).empty:
                    with st.expander("⚠️ D8 terminals not connected", expanded=True):
                        st.dataframe(_master_design["unconnected_register"], use_container_width=True, hide_index=True)
                for _warn in _master_design.get("warnings", []):
                    st.caption(f"• {_warn}")
                if isinstance(_master_design.get("gravity_screen_register"), pd.DataFrame) and not _master_design["gravity_screen_register"].empty:
                    with st.expander("📐 Preliminary gravity level-head screen", expanded=False):
                        st.dataframe(_master_design["gravity_screen_register"], use_container_width=True, hide_index=True)
                st.download_button(
                    "⬇️ Download main collector connection register CSV",
                    data=_master_design.get("connection_register", pd.DataFrame()).to_csv(index=False).encode("utf-8"),
                    file_name="main_collector_connection_register.csv", mime="text/csv",
                    key="p2_download_main_collector_connections",
                )
            else:
                st.caption("No master collector has been generated yet. Choose a suggested or user-selected external outfall and generate the collector network.")

            _d1, _d2, _d3, _d4, _d5 = st.columns(5)
            _d1.metric("Integrated channel length", f"{_dash_payload['channel_length_km']:.2f} km")
            _d2.metric("Channel features", f"{_dash_payload['channel_feature_count']:,}", help=f"{_dash_payload['main_channel_features']} main + {_dash_payload['secondary_channel_features']} secondary")
            _d3.metric("D8 route networks", f"{_dash_payload['network_count']:,}")
            _d4.metric("Boundary outfall candidates", f"{_dash_payload['boundary_outfall_candidates']:,}")
            _d5.metric("Internal stops", f"{_dash_payload['interior_sink_or_coverage_stops']:,}")

            _x1, _x2, _x3 = st.columns(3)
            _x1.metric("Road / drain intersection candidates", f"{_dash_payload['road_crossing_candidates']:,}")
            _x2.metric("Post-grading D8 links", f"{_dash_payload['d8_link_count']:,}")
            _max_route_q = float(_dash_route_df["screening_peak_flow_m3s"].max()) if not _dash_route_df.empty else float("nan")
            _x3.metric("Largest route peak-flow screen", f"{_max_route_q:.3f} m³/s" if pd.notna(_max_route_q) else "Not available", help="Maximum individual route screening estimate. It is not a combined design discharge or a channel capacity check.")

            with st.expander("🧭 Drainage route register – upstream branches to terminal", expanded=True):
                if _dash_route_df.empty:
                    st.warning("No retained D8 route components are available. Re-run Page-1 flow screening and grading.")
                else:
                    _route_display_cols = [
                        "network_id", "headwater_starts", "main_links", "secondary_links",
                        "total_links", "route_length_m", "max_accumulation_cells",
                        "approx_upstream_area_ha", "screening_peak_flow_m3s",
                        "terminal_ids", "terminal_status",
                    ]
                    st.dataframe(_dash_route_df[_route_display_cols], use_container_width=True, hide_index=True)
                    st.caption(
                        "Approx. upstream area = maximum accumulated grid cells × grid-cell area. "
                        "Peak flow = C × I × A / 360, with A in hectares and I in mm/hour. "
                        "Use as a route-ranking indicator only; it is not a delineated catchment or verified hydraulic discharge."
                    )
                    with st.expander("➡️ Read the drainage flow trace", expanded=False):
                        for _route in _dash_route_df.itertuples(index=False):
                            _terminal_text = str(_route.terminal_ids)
                            _status_text = str(_route.terminal_status)
                            st.write(
                                f"**{_route.network_id}** — {int(_route.headwater_starts)} upstream branch start(s) "
                                f"→ {_route.route_length_m:.0f} m connected D8 route "
                                f"→ **{_terminal_text}** ({_status_text}); "
                                f"approx. upstream area {_route.approx_upstream_area_ha:.2f} ha."
                            )
                    _route_chart_a, _route_chart_b = st.columns(2)
                    with _route_chart_a:
                        st.caption("Connected D8 route length by network")
                        st.bar_chart(_dash_route_df.set_index("network_id")[["route_length_m"]])
                    with _route_chart_b:
                        st.caption("Approximate upstream area at maximum accumulation")
                        st.bar_chart(_dash_route_df.set_index("network_id")[["approx_upstream_area_ha"]])
                    st.download_button(
                        "⬇️ Download drainage route register CSV",
                        data=_dash_route_df.to_csv(index=False).encode("utf-8"),
                        file_name="drainage_route_register.csv", mime="text/csv",
                        key="p2_download_drainage_route_register",
                    )

            with st.expander("🚪 Where does each route end? – terminal / outfall register", expanded=True):
                if _dash_terminal_df.empty:
                    st.info("No terminal candidates were exported for the current D8 network.")
                else:
                    st.dataframe(_dash_terminal_df, use_container_width=True, hide_index=True)
                    st.warning(
                        "Boundary-edge candidates must be checked against the actual receiving drain, stream or permitted discharge point. "
                        "Interior sink / coverage-stop points require field review to distinguish real depressions from survey NoData or threshold termination."
                    )
                    st.download_button(
                        "⬇️ Download terminal / outfall register CSV",
                        data=_dash_terminal_df.to_csv(index=False).encode("utf-8"),
                        file_name="drainage_terminal_register.csv", mime="text/csv",
                        key="p2_download_drainage_terminal_register",
                    )

            with st.expander("🛣️ Road crossings and potential culvert locations", expanded=False):
                if _dash_crossing_df.empty:
                    st.info("No point intersections were found between the current Page-2 road centre-lines and integrated D8 channel lines. This does not prove that crossings are unnecessary.")
                else:
                    st.dataframe(_dash_crossing_df, use_container_width=True, hide_index=True)
                    st.warning("These are geometric crossing candidates only. Culvert / cross-drain need and size require local levels, contributing catchment and hydraulic checks.")
                    st.download_button(
                        "⬇️ Download road-crossing candidate CSV",
                        data=_dash_crossing_df.to_csv(index=False).encode("utf-8"),
                        file_name="drainage_road_crossing_candidates.csv", mime="text/csv",
                        key="p2_download_drainage_crossings",
                    )

            with st.expander("🏞️ Drainage channels by solar site", expanded=False):
                if _dash_site_df.empty:
                    st.info("Site polygons or Page-2 drainage geometries are unavailable.")
                else:
                    st.dataframe(_dash_site_df, use_container_width=True, hide_index=True)
                    st.caption("Channel length is measured inside each site polygon. It is not the same as the full upstream catchment area draining through that site.")

        except Exception as _dash_exc:
            logger.exception("Page-2 drainage dashboard failed")
            st.warning(f"Drainage dashboard could not be generated: {_dash_exc}")
    else:
        st.info("Generate Page-1 post-grading D8 flow paths, then generate Page-2 infrastructure to populate the drainage dashboard.")

    # -------------------------------------------------
    # Apply the most recently drawn internal exclusion
    # -------------------------------------------------
    try:
        _drawings = (_map_result or {}).get("all_drawings") if isinstance(_map_result, dict) else None
        if isinstance(_drawings, dict):
            _features = _drawings.get("features", [])
        elif isinstance(_drawings, list):
            _features = _drawings
        else:
            _features = []

        if _features and _edit_site_ids:
            _feature = _features[-1]
            _feature_sig = json.dumps(_feature, sort_keys=True, default=str)
            _already_applied = st.session_state.get("page2_last_internal_edit_sig") == _feature_sig

            if not _already_applied:
                _selected_site_id = st.session_state.get("page2_internal_edit_site_id", _edit_site_ids[0])
                _target_site_id = _selected_site_id
                st.subheader("✂️ Apply Internal Area Removal")
                st.caption(
                    f"Pending drawing. Selected site: **{_selected_site_id}**. "
                    "The site with the largest overlap with your drawing will be edited."
                )

                if st.button("✂️ Remove Drawn Area From Site", key="page2_apply_internal_cut", type="secondary"):
                    try:
                        _draw_geom_wgs = shape(_feature.get("geometry", _feature))
                        if _draw_geom_wgs.is_empty or not _draw_geom_wgs.is_valid:
                            _draw_geom_wgs = _draw_geom_wgs.buffer(0)
                        if _draw_geom_wgs.is_empty:
                            raise ValueError("The drawn area is empty or invalid.")

                        _sites_utm_edit = park.get("sites_utm")
                        if _sites_utm_edit is None or _sites_utm_edit.empty:
                            raise ValueError("UTM site geometry is unavailable.")

                        _draw_utm = gpd.GeoSeries([_draw_geom_wgs], crs="EPSG:4326").to_crs(epsg=utm_epsg).iloc[0]

                        # Identify the site actually covered by the drawing. This
                        # avoids false errors when the user draws on SITE_02 while
                        # the dropdown is still on SITE_01 (or vice versa).
                        _overlap_candidates = []
                        for _candidate_idx, _candidate_row in _sites_utm_edit.iterrows():
                            _candidate_geom = _candidate_row.geometry
                            if _candidate_geom is None or _candidate_geom.is_empty:
                                continue
                            _candidate_cut = _candidate_geom.intersection(_draw_utm)
                            _candidate_area = float(_candidate_cut.area) if not _candidate_cut.is_empty else 0.0
                            _overlap_candidates.append((
                                _candidate_area,
                                1 if str(_candidate_row.get("site_id")) == str(_selected_site_id) else 0,
                                _candidate_idx,
                                str(_candidate_row.get("site_id")),
                            ))

                        if not _overlap_candidates:
                            raise ValueError("No valid site geometries are available for internal area removal.")

                        _overlap_candidates.sort(key=lambda _x: (_x[0], _x[1]), reverse=True)
                        _best_overlap, _, _row_idx, _target_site_id = _overlap_candidates[0]
                        _old_geom = _sites_utm_edit.at[_row_idx, "geometry"]
                        _old_area = float(_old_geom.area)
                        _cut = _old_geom.intersection(_draw_utm)
                        _cut_area = float(_cut.area) if not _cut.is_empty else 0.0

                        if _cut_area <= 1.0:
                            raise ValueError(
                                "The drawn area does not overlap any site by more than 1 m². "
                                f"Selected site was {_selected_site_id}; draw inside a green site area."
                            )

                        if str(_target_site_id) != str(_selected_site_id):
                            st.info(
                                f"Drawing overlaps **{_target_site_id}** most strongly, so **{_target_site_id}** "
                                f"will be edited instead of the selected **{_selected_site_id}**."
                            )
                        if _cut_area >= _old_area * 0.999999:
                            raise ValueError(
                                "The drawing would remove essentially the whole site. "
                                "Use the site-size controls if the entire site must be regenerated."
                            )

                        _new_geom = _old_geom.difference(_draw_utm)
                        if _new_geom.is_empty:
                            raise ValueError("The edit would leave the site with no land.")
                        if not _new_geom.is_valid:
                            _new_geom = _new_geom.buffer(0)
                        if _new_geom.is_empty:
                            raise ValueError("The edited site geometry is invalid.")

                        _new_area = float(_new_geom.area)
                        _area_acres = _new_area / 4047.0
                        _old_ac_mw = float(_sites_utm_edit.at[_row_idx, "ac_mw"])
                        _old_dc_mw = float(_sites_utm_edit.at[_row_idx, "dc_mw"])
                        _area_ratio = _new_area / _old_area
                        _new_ac_mw = _old_ac_mw * _area_ratio
                        _new_dc_mw = _old_dc_mw * _area_ratio

                        _sites_utm_edit = _sites_utm_edit.copy()
                        _sites_utm_edit.at[_row_idx, "geometry"] = _new_geom
                        _sites_utm_edit.at[_row_idx, "area_acres"] = round(_area_acres, 2)
                        _sites_utm_edit.at[_row_idx, "ac_mw"] = round(_new_ac_mw, 2)
                        _sites_utm_edit.at[_row_idx, "dc_mw"] = round(_new_dc_mw, 2)
                        _sites_utm_edit.at[_row_idx, "site_status"] = "MANUALLY_EDITED"

                        _spine_utm_edit = park.get("spine_utm")
                        if _spine_utm_edit is None or _spine_utm_edit.is_empty:
                            raise ValueError("Spine geometry is unavailable.")

                        _spurs_utm_edit = []
                        _spur_endpoints_utm_edit = []
                        for _, _sr in _sites_utm_edit.iterrows():
                            _g = _sr.geometry
                            if _g is None or _g.is_empty:
                                continue
                            _site_center = _g.centroid
                            _spine_point = _spine_utm_edit.interpolate(_spine_utm_edit.project(_site_center))
                            try:
                                _, _near_pt = nearest_points(_spine_point, _g.boundary)
                                _spur_end = _near_pt
                            except Exception:
                                _spur_end = _site_center
                            _spurs_utm_edit.append(LineString([_spine_point, _spur_end]))
                            _spur_endpoints_utm_edit.append(_spur_end)

                        _old_roads_wgs = park.get("roads", {}) or {}
                        _old_main_access_wgs = _old_roads_wgs.get("main_access", [])
                        _to_utm_edit = lambda _g: gpd.GeoSeries([_g], crs="EPSG:4326").to_crs(epsg=utm_epsg).iloc[0]

                        # ``main_access`` may be stored as a list, GeoSeries, or
                        # (in older session-state layouts) a single Shapely geometry.
                        # Do not use ``if _old_main_access_wgs`` here because a
                        # GeoSeries has an ambiguous truth value in pandas.
                        _main_access_geom = None
                        if isinstance(_old_main_access_wgs, gpd.GeoSeries):
                            _valid_access = _old_main_access_wgs.dropna()
                            _valid_access = _valid_access[~_valid_access.is_empty]
                            if not _valid_access.empty:
                                _main_access_geom = _valid_access.iloc[0]
                        elif isinstance(_old_main_access_wgs, (list, tuple)):
                            for _candidate_access in _old_main_access_wgs:
                                if _candidate_access is not None and not getattr(_candidate_access, "is_empty", True):
                                    _main_access_geom = _candidate_access
                                    break
                        elif _old_main_access_wgs is not None and not getattr(_old_main_access_wgs, "is_empty", True):
                            _main_access_geom = _old_main_access_wgs

                        _main_access_utm_edit = (
                            [_to_utm_edit(_main_access_geom)]
                            if _main_access_geom is not None else []
                        )
                        _roads_utm_edit = {
                            "main_access": _main_access_utm_edit,
                            "spine": [_spine_utm_edit],
                            "site_spurs": _spurs_utm_edit,
                            "spur_endpoints": _spur_endpoints_utm_edit,
                        }

                        _gen_edit = ParkInfrastructureGenerator()
                        _boundary_utm_edit = gpd.GeoSeries([boundary], crs="EPSG:4326").to_crs(epsg=utm_epsg).iloc[0]
                        _drainage_utm_edit = _gen_edit._generate_drainage(
                            _sites_utm_edit, _roads_utm_edit,
                            flow_grid=_page2_drainage_flow_grid,
                            boundary=_boundary_utm_edit,
                        )
                        _electrical_utm_edit = _gen_edit._generate_electrical(_sites_utm_edit, _roads_utm_edit, _boundary_utm_edit)

                        def _wgs_list_edit(_items):
                            return [
                                gpd.GeoSeries([_g], crs=f"EPSG:{utm_epsg}").to_crs(epsg=4326).iloc[0]
                                for _g in _items
                                if _g is not None and not _g.is_empty
                            ]

                        _remaining_wgs_edit = _sites_utm_edit.to_crs(epsg=4326)
                        _roads_wgs_edit = {
                            "main_access": _wgs_list_edit(_roads_utm_edit["main_access"]),
                            "spine": _wgs_list_edit(_roads_utm_edit["spine"]),
                            "site_spurs": _wgs_list_edit(_roads_utm_edit["site_spurs"]),
                        }
                        _drainage_wgs_edit = {
                            "channels": _wgs_list_edit(_drainage_utm_edit.get("channels", [])),
                            "main_channels": _wgs_list_edit(_drainage_utm_edit.get("main_channels", [])),
                            "secondary_channels": _wgs_list_edit(_drainage_utm_edit.get("secondary_channels", [])),
                            "source": _drainage_utm_edit.get("source", "legacy_road_spur_fallback"),
                            "method": _drainage_utm_edit.get("method", ""),
                            "channel_count": int(_drainage_utm_edit.get("channel_count", len(_drainage_utm_edit.get("channels", [])))),
                            "main_channel_count": int(_drainage_utm_edit.get("main_channel_count", 0)),
                            "secondary_channel_count": int(_drainage_utm_edit.get("secondary_channel_count", 0)),
                        }
                        _electrical_wgs_edit = {
                            "oh_line_33kv": _wgs_list_edit(_electrical_utm_edit.get("oh_line_33kv", [])),
                            "droppers": _wgs_list_edit(_electrical_utm_edit.get("droppers", [])),
                            "evacuation_line": _wgs_list_edit(_electrical_utm_edit.get("evacuation_line", [])),
                            "poles": _wgs_list_edit(_electrical_utm_edit.get("poles", [])),
                            "site_pole_yards": _wgs_list_edit(_electrical_utm_edit.get("site_pole_yards", [])),
                        }

                        _usable_edit = float(park.get("usable_area_utm").area)
                        _assigned_edit = float(_sites_utm_edit.geometry.union_all().area)
                        _residual_edit = max(0.0, _usable_edit - _assigned_edit)
                        _sum_edit = float(_sites_utm_edit.geometry.area.sum())
                        _overlap_edit = max(0.0, _sum_edit - _assigned_edit)

                        _summary_edit = dict(park.get("park_summary", {}))
                        _summary_edit.update(_gen_edit._calculate_infra_summary(
                            _roads_utm_edit, _drainage_utm_edit, _electrical_utm_edit
                        ))
                        _summary_edit.update({
                            "site_count": int(len(_sites_utm_edit)),
                            "site_capacity_mw": round(float(_sites_utm_edit["ac_mw"].sum()), 2),
                            "usable_area_acres": round(_usable_edit / 4047.0, 2),
                            "site_coverage_pct": round((_assigned_edit / _usable_edit * 100.0) if _usable_edit else 0.0, 2),
                            "unallocated_area_acres": round(_residual_edit / 4047.0, 2),
                        })

                        park["sites_utm"] = _sites_utm_edit
                        park["sites"] = _remaining_wgs_edit
                        park["roads"] = _roads_wgs_edit
                        park["drainage"] = _drainage_wgs_edit
                        park["electrical"] = _electrical_wgs_edit
                        park["park_summary"] = _summary_edit

                        # Keep Page-3's committed capacity/configuration state in
                        # sync with the edited Page-2 geometry. Page-3's map uses
                        # site_config_df for its labels/areas, so leaving that
                        # dataframe untouched would make an edited site appear
                        # unchanged after navigation to Page-3.
                        _cfg_df_edit = park.get("site_config_df")
                        if isinstance(_cfg_df_edit, pd.DataFrame) and not _cfg_df_edit.empty:
                            _cfg_df_edit = _cfg_df_edit.copy()
                            _cfg_mask = _cfg_df_edit["site_id"].astype(str) == str(_target_site_id)
                            if _cfg_mask.any():
                                _cfg_df_edit.loc[_cfg_mask, "area_acres"] = round(_area_acres, 2)
                                _cfg_df_edit.loc[_cfg_mask, "max_ac_mw"] = round(_area_acres / float(park.get("park_summary", {}).get("acres_per_mw", 2.5)), 2)
                                _cfg_df_edit.loc[_cfg_mask, "user_ac_mw"] = round(_new_ac_mw, 2)
                                _cfg_df_edit.loc[_cfg_mask, "dc_mw_calc"] = round(_new_dc_mw, 2)
                                _cfg_df_edit.loc[_cfg_mask, "user_dc_ac"] = round((_new_dc_mw / _new_ac_mw) if _new_ac_mw > 0 else 1.30, 2)
                                _cfg_df_edit.loc[_cfg_mask, "status"] = "✏️ MANUALLY EDITED"
                            park["site_config_df"] = _cfg_df_edit

                        # Mark the geometry as manually edited. Page-3 must not
                        # regenerate the original full envelope and silently undo
                        # this land removal.
                        park["geometry_manual_edited"] = True
                        park["geometry_edit_version"] = int(park.get("geometry_edit_version", 0)) + 1
                        park["editor_version"] = int(park.get("editor_version", 0)) + 1

                        if park.get("site_targets"):
                            for _t in park["site_targets"]:
                                if str(_t.get("site_id")) == str(_target_site_id):
                                    _t["ac_mw"] = round(_new_ac_mw, 2)
                                    _t["area_acres"] = round(_area_acres, 2)

                        park.setdefault("manual_site_area_edits", []).append({
                            "site_id": str(_target_site_id),
                            "removed_area_acres": round(_cut_area / 4047.0, 2),
                            "remaining_area_acres": round(_area_acres, 2),
                            "remaining_ac_mw": round(_new_ac_mw, 2),
                        })
                        park["infrastructure_complete"] = True
                        park["infrastructure_done"] = True
                        st.session_state["park_data"] = park
                        st.session_state["page2_last_internal_edit_sig"] = _feature_sig
                        st.success(
                            f"✅ Removed {_cut_area / 4047.0:.2f} acres from {_target_site_id}. "
                            f"Remaining site area: {_area_acres:.2f} acres ({_new_ac_mw:.2f} MW AC)."
                        )
                        st.rerun()
                    except Exception as _cut_err:
                        logger.exception("Internal site-area deletion failed")
                        st.error(f"❌ Internal area removal failed: {_cut_err}")
    except Exception as _draw_err:
        st.warning(f"Drawing/edit control unavailable: {_draw_err}")

else:
    st.info("👆 Generate park infrastructure after completing Page-1")
