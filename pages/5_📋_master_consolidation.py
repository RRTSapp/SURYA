import streamlit as st
import pandas as pd
import geopandas as gpd
import folium
from streamlit_folium import st_folium
from io import BytesIO
import datetime
import logging

logger = logging.getLogger(__name__)

# -------------------------------------------------
# Page config
# -------------------------------------------------
st.set_page_config(
    page_title="Master Consolidation",
    page_icon="📋",
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

st.title("📋 Master Consolidation Report")

# -------------------------------------------------
# Session guard
# -------------------------------------------------
park = st.session_state.get("park_data")

if park is None:
    st.error("❌ Session not initialised. Start from app.py")
    st.stop()

if not park.get("sites_generated"):
    st.warning("⚠️ Complete Page-3 (Freeze Configuration) before consolidation.")
    st.stop()

# -------------------------------------------------
# Data assembly
# -------------------------------------------------
sites_gdf      = park.get("sites")           # GeoDataFrame (WGS84)
site_config    = park.get("site_config", {}) # frozen Page-3 capacity
site_layouts   = park.get("site_layouts", {})# Page-4 design results
boundary       = park.get("boundary")
roads          = park.get("roads", {}) or {}
electrical     = park.get("electrical", {}) or {}
facilities     = park.get("facilities", {}) or {}
boundary_map   = park.get("boundary")        # WGS84 boundary for maps + PDF
park_summary   = park.get("park_summary", {})

all_site_ids   = list(site_config.keys())
designed_ids   = list(site_layouts.keys())
pending_ids    = [s for s in all_site_ids if s not in designed_ids]

total_sites    = len(all_site_ids)
designed_count = len(designed_ids)
pending_count  = len(pending_ids)

# Park-level capacity from Page-3
total_ac_mw = sum(v.get("ac_mw", 0) for v in site_config.values())
total_dc_mw = sum(v.get("dc_mw", 0) for v in site_config.values())
total_acres  = sum(v.get("area_acres", 0) for v in site_config.values())

# -------------------------------------------------
# Geometry-derived quantities (boundary + fencing)
# -------------------------------------------------
# 1. Compound wall = park outer boundary perimeter (m → rounded to 0 dec)
# 2. Chainlink fence = sum of internal site perimeters MINUS shared edges
#    Approximation: total site perimeters - 2 × shared boundary with park
#    (exact shared-edge computation is expensive; we subtract park perimeter
#     once since sites tile the park interior)
_compound_wall_m   = 0.0
_chainlink_fence_m = 0.0

try:
    if boundary is not None:
        _bnd_utm = gpd.GeoSeries([boundary], crs=4326).to_crs(
            epsg=park.get("utm_epsg", 4326)
        ).iloc[0]
        _compound_wall_m = round(_bnd_utm.length, 1)

    if sites_gdf is not None and not sites_gdf.empty:
        _utm_epsg = park.get("utm_epsg", 4326)
        _sites_utm = sites_gdf.to_crs(epsg=_utm_epsg)
        _total_site_perim = 0.0
        for geom in _sites_utm.geometry:
            if geom is None or geom.is_empty:
                continue
            g = max(geom.geoms, key=lambda x: x.area) if geom.geom_type == "MultiPolygon" else geom
            _total_site_perim += g.length
        # Internal fences = site perimeters minus park outer boundary
        # (outer edges are compound wall, not chainlink)
        _chainlink_fence_m = round(max(0, _total_site_perim - _compound_wall_m), 1)
except Exception as _fence_err:
    pass  # Falls back to 0 — user can enter manually

# =============================================================
# SECTION 1 — PARK PROGRESS DASHBOARD
# =============================================================
st.markdown("---")
st.header("📊 Park Progress Dashboard")

# Progress bar
progress_pct = designed_count / total_sites if total_sites else 0
col_p, col_m = st.columns([3, 1])
with col_p:
    st.progress(progress_pct)
    st.caption(f"{designed_count} of {total_sites} sites designed in Page-4")
with col_m:
    if designed_count == total_sites:
        st.success("✅ All sites designed")
    else:
        st.warning(f"⏳ {pending_count} site(s) pending layout")

# Key metrics
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Sites",    total_sites)
c2.metric("Total AC",       f"{total_ac_mw:.2f} MW")
c3.metric("Total DC",       f"{total_dc_mw:.2f} MW")
c4.metric("Avg DC/AC",      f"{(total_dc_mw/total_ac_mw):.2f}" if total_ac_mw else "—")
c5.metric("Total Area",     f"{total_acres:.1f} ac")

# =============================================================
# SECTION 2 — SITE-BY-SITE SUMMARY TABLE
# =============================================================
st.markdown("---")
st.header("🧩 Site-by-Site Summary")

rows = []
for sid in all_site_ids:
    cfg = site_config.get(sid, {})
    lyt = site_layouts.get(sid, {})
    metrics = lyt.get("metrics", {}) if lyt else {}

    is_designed = sid in designed_ids

    rows.append({
        "Site ID":          sid,
        "Status":           "✅ Designed" if is_designed else "⏳ Pending",
        "Area (ac)":        round(cfg.get("area_acres", 0), 2),
        "AC (MW)":          round(cfg.get("ac_mw", 0), 2),
        "DC (MW)":          round(cfg.get("dc_mw", 0), 2),
        "DC/AC":            round(cfg.get("dc_ac_ratio", 0), 2),
        "Tables":           metrics.get("tables", "—"),
        "Inverters":        metrics.get("inverters", "—"),
        "DC Cable (km)":    round(metrics.get("total_dc_km", 0), 3) if is_designed else "—",
        "AC Cable (km)":    round(metrics.get("total_ac_km", 0), 3) if is_designed else "—",
        "Placement":        metrics.get("placement_quality", "—"),
    })

summary_df = pd.DataFrame(rows)
st.dataframe(summary_df, use_container_width=True, hide_index=True)

# Park totals row
designed_rows = [r for r in rows if r["Status"] == "✅ Designed"]
if designed_rows:
    st.caption(
        f"**Designed sites total** — "
        f"Tables: {sum(r['Tables'] for r in designed_rows if isinstance(r['Tables'], int))} | "
        f"Inverters: {sum(r['Inverters'] for r in designed_rows if isinstance(r['Inverters'], int))} | "
        f"DC Cable: {sum(r['DC Cable (km)'] for r in designed_rows if isinstance(r['DC Cable (km)'], float)):.3f} km | "
        f"AC Cable: {sum(r['AC Cable (km)'] for r in designed_rows if isinstance(r['AC Cable (km)'], float)):.3f} km"
    )

# =============================================================
# SECTION 3 — RATE SHEET (single source of truth)
# Reads/writes park["rate_sheet"] — shared with Page-4
# =============================================================
st.markdown("---")
st.header("💰 Rate Sheet")
st.caption("Rates are shared across all pages. Changes here update the central rate sheet.")

# Ensure rate_sheet exists (backward compat if session started before app.py update)
if "rate_sheet" not in park:
    park["rate_sheet"] = {
        'module_per_wp': 15.0, 'inverter_per_kw': 1500.0,
        'dc_cable_per_m': 80.0, 'ac_cable_per_m': 150.0,
        'mounting_per_wp': 2.0, 'civil_per_mw': 15.0,
        'site_electrical_per_mw': 10.0,
        'road_per_km': 10.0, 'drainage_per_km': 5.0,
        'culvert_each': 50000.0, 'compound_wall_rm': 800.0,
        'chainlink_rm': 250.0, 'gate_lump': 5.0,
        'oh_line_per_km': 15.0, 'pole_each': 25000.0,
        'site_pole_yard': 1500000.0, 'switchyard_lump': 30.0,
        'evac_line_per_km': 15.0, 'pooling_ss_lump': 200.0,
        'row_land_per_km': 2.0, 'row_clear_per_km': 1.0,
        'land_per_acre': 5.0,
        'approvals_lump': 50.0,
    }

rs = park["rate_sheet"]
rs.setdefault('land_per_acre',   5.0)
rs.setdefault('approvals_lump', 50.0)

with st.expander("📝 Edit Rate Sheet", expanded=True):
    st.caption("**Site-Level Rates**")
    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    with r1c1:
        rs['module_per_wp']          = st.number_input("Solar Module (₹/Wp)",          value=float(rs['module_per_wp']),          step=0.5,     min_value=0.0)
        rs['inverter_per_kw']        = st.number_input("Inverter (₹/kW AC)",           value=float(rs['inverter_per_kw']),        step=100.0,   min_value=0.0)
    with r1c2:
        rs['dc_cable_per_m']         = st.number_input("DC Cable (₹/m)",               value=float(rs['dc_cable_per_m']),         step=5.0,     min_value=0.0)
        rs['ac_cable_per_m']         = st.number_input("AC Cable (₹/m)",               value=float(rs['ac_cable_per_m']),         step=5.0,     min_value=0.0)
    with r1c3:
        rs['mounting_per_wp']        = st.number_input("Mounting Structure (₹/Wp)",    value=float(rs['mounting_per_wp']),        step=0.5,     min_value=0.0)
        rs['civil_per_mw']           = st.number_input("Civil Works (₹ Lakh/MW AC)",   value=float(rs['civil_per_mw']),           step=1.0,     min_value=0.0)
    with r1c4:
        rs['site_electrical_per_mw'] = st.number_input("Site Electrical (₹ Lakh/MW AC)", value=float(rs['site_electrical_per_mw']), step=1.0, min_value=0.0)
        rs['land_per_acre']          = st.number_input("Land Cost (₹ Lakh/acre)",         value=float(rs['land_per_acre']),          step=0.5, min_value=0.0,
                                                        help="Project land cost per acre. Area auto-computed from site boundary polygon.")
        rs['approvals_lump']         = st.number_input("Statutory Approvals (₹ Lakh LS)", value=float(rs['approvals_lump']),         step=5.0, min_value=0.0,
                                                        help="Lump sum per site for CEA, DISCOM, forest, environment clearances.")

    st.markdown("---")
    st.caption("**Park Common Infrastructure Rates**")
    p1c1, p1c2, p1c3, p1c4 = st.columns(4)
    with p1c1:
        rs['road_per_km']        = st.number_input("Internal Road (₹ Lakh/km)",            value=float(rs['road_per_km']),        step=1.0,     min_value=0.0)
        rs['drainage_per_km']    = st.number_input("Drainage Channel (₹ Lakh/km)",         value=float(rs['drainage_per_km']),    step=0.5,     min_value=0.0)
        rs['culvert_each']       = st.number_input("Culvert / Crossing (₹ each)",          value=float(rs['culvert_each']),       step=5000.0,  min_value=0.0)
    with p1c2:
        rs['oh_line_per_km']     = st.number_input("33kV Internal OH Line (₹ Lakh/km)",    value=float(rs['oh_line_per_km']),     step=1.0,     min_value=0.0)
        rs['pole_each']          = st.number_input("33kV Pole (₹ each)",                   value=float(rs['pole_each']),          step=1000.0,  min_value=0.0)
        rs['site_pole_yard']     = st.number_input("Site Pole Yard / 4-Pole Sub (₹ each)", value=float(rs['site_pole_yard']),     step=10000.0, min_value=0.0)
    with p1c3:
        rs['switchyard_lump']    = st.number_input("33kV Switchyard / Pooling Stn (₹ Lakh)", value=float(rs['switchyard_lump']), step=5.0,     min_value=0.0)
        rs['gate_lump']          = st.number_input("Main Gate (₹ Lakh)",                   value=float(rs['gate_lump']),          step=0.5,     min_value=0.0)
        rs['compound_wall_rm']   = st.number_input("Compound Wall (₹/rm)",                 value=float(rs['compound_wall_rm']),   step=50.0,    min_value=0.0,
                                                   help=f"Auto qty: {_compound_wall_m:.0f} rm (from park boundary)")
        rs['chainlink_rm']       = st.number_input("Chainlink Fence — internal (₹/rm)",    value=float(rs['chainlink_rm']),       step=10.0,    min_value=0.0,
                                                   help=f"Auto qty: {_chainlink_fence_m:.0f} rm (from site boundaries)")
    with p1c4:
        rs['row_land_per_km']    = st.number_input("ROW Land Acquisition (₹ Lakh/km)",     value=float(rs['row_land_per_km']),    step=1.0,     min_value=0.0)
        rs['row_clear_per_km']   = st.number_input("ROW Clearance / Tree Cutting (₹ Lakh/km)", value=float(rs['row_clear_per_km']), step=0.5,   min_value=0.0)

    st.markdown("---")
    st.caption("**Evacuation Infrastructure**")

    evac_col1, evac_col2 = st.columns([2, 3])
    with evac_col1:
        evac_scenario = st.selectbox(
            "Evacuation Scenario",
            [
                "33kV — Direct to EB Substation",
                "110kV — Via 110kV Pooling Station",
            ],
            help=(
                "Select based on DISCOM/SLDC approval. "
                "33kV direct: park switchyard → EB SS. "
                "110kV: park switchyard → 110kV pooling SS → EB SS via 110kV."
            )
        )

    with evac_col2:
        if evac_scenario.startswith("33kV"):
            st.caption("📍 Enter coordinates of EB Substation (destination of 33kV evacuation line)")
            ev1, ev2 = st.columns(2)
            evac_ss_lat = ev1.number_input("EB Substation Latitude",  value=0.0, format="%.6f", key="evac_lat")
            evac_ss_lon = ev2.number_input("EB Substation Longitude", value=0.0, format="%.6f", key="evac_lon")
            pooling_ss_lat = None
            pooling_ss_lon = None
        else:
            st.caption("📍 Enter coordinates of 110kV Pooling Station")
            ev1, ev2 = st.columns(2)
            pooling_ss_lat = ev1.number_input("110kV Pooling SS Latitude",  value=0.0, format="%.6f", key="pool_lat")
            pooling_ss_lon = ev2.number_input("110kV Pooling SS Longitude", value=0.0, format="%.6f", key="pool_lon")
            evac_ss_lat = None
            evac_ss_lon = None

    # Compute evacuation line length from switchyard to destination SS
    _evac_line_km = 0.0
    _evac_dest_lat = pooling_ss_lat if evac_scenario.startswith("110kV") else evac_ss_lat
    _evac_dest_lon = pooling_ss_lon if evac_scenario.startswith("110kV") else evac_ss_lon
    _park_cx = boundary.centroid.x if boundary is not None else 0.0
    _park_cy = boundary.centroid.y if boundary is not None else 0.0

    if _evac_dest_lat and _evac_dest_lon and (_evac_dest_lat != 0.0 or _evac_dest_lon != 0.0):
        from math import radians, sin, cos, sqrt, atan2
        R = 6371.0
        lat1, lon1 = radians(_park_cy), radians(_park_cx)
        lat2, lon2 = radians(_evac_dest_lat), radians(_evac_dest_lon)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
        _evac_line_km = round(2 * R * atan2(sqrt(a), sqrt(1 - a)), 3)
        st.caption(f"📏 Straight-line evacuation distance: **{_evac_line_km:.2f} km**")
    else:
        _evac_line_km = 0.0
        st.caption("ℹ️ Enter SS coordinates above to auto-calculate evacuation line length.")

    evac_voltage_label = "33kV" if evac_scenario.startswith("33kV") else "110kV"
    evac_col3, evac_col4, evac_col5 = st.columns(3)
    with evac_col3:
        _evac_default = 25.0 if evac_scenario.startswith("110kV") else float(rs['evac_line_per_km'])
        rs['evac_line_per_km'] = st.number_input(
            f"Evacuation OH Line — {evac_voltage_label} (₹ Lakh/km)",
            value=_evac_default, step=1.0, min_value=0.0, key="rate_evac_line"
        )
    with evac_col4:
        if evac_scenario.startswith("110kV"):
            rs['pooling_ss_lump'] = st.number_input(
                "110kV Pooling Station (₹ Lakh LS)",
                value=float(rs['pooling_ss_lump']), step=10.0, min_value=0.0, key="rate_pool_ss"
            )
        else:
            rs['pooling_ss_lump'] = 0.0
    with evac_col5:
        evac_line_km_override = st.number_input(
            "Evacuation Line Length (km) — override if needed",
            value=float(_evac_line_km), step=0.1, min_value=0.0, key="evac_km_override"
        )
    evac_line_km = evac_line_km_override

# =============================================================
# SECTION 4 — COST ESTIMATION
# =============================================================
st.markdown("---")
st.header("📈 Cost Estimation")

# -----------------------------------------------------------------
# Helper: extract header-level qty from Page-4 bom_df by Section
# bom_df columns: Section, Description, Unit, Qty, Rate, Total,
#                 GST %, GST Value, Total (Incl. GST)
# -----------------------------------------------------------------
def _bom_section_total(bom_df, section_keywords):
    """
    Sum 'Total' column matching section_keywords against BOTH
    'Section' AND 'Description' columns (case-insensitive).
    Falls back to 'Total (Incl. GST)' if 'Total' is zero.
    Works regardless of BOMGenerator section naming convention.
    """
    if bom_df is None or bom_df.empty:
        return 0.0
    kws = [k.upper() for k in section_keywords]
    has_section = "Section" in bom_df.columns
    has_desc    = "Description" in bom_df.columns
    if not has_section and not has_desc:
        return 0.0
    def _row_matches(row):
        sec  = str(row["Section"]).upper()     if has_section else ""
        desc = str(row["Description"]).upper() if has_desc    else ""
        return any(k in sec or k in desc for k in kws)
    mask = bom_df.apply(_row_matches, axis=1)
    if "Total" in bom_df.columns:
        val = float(bom_df.loc[mask, "Total"].fillna(0).sum())
        if val != 0:
            return val
    if "Total (Incl. GST)" in bom_df.columns:
        return float(bom_df.loc[mask, "Total (Incl. GST)"].fillna(0).sum())
    return 0.0

def _bom_section_qty(bom_df, description_keywords, unit_filter=None):
    """Sum 'Qty' matching keywords against Section and Description."""
    if bom_df is None or bom_df.empty:
        return 0.0
    kws = [k.upper() for k in description_keywords]
    has_section = "Section" in bom_df.columns
    has_desc    = "Description" in bom_df.columns
    def _row_matches(row):
        sec  = str(row["Section"]).upper()     if has_section else ""
        desc = str(row["Description"]).upper() if has_desc    else ""
        return any(k in sec or k in desc for k in kws)
    mask = bom_df.apply(_row_matches, axis=1)
    if unit_filter:
        mask = mask & (bom_df["Unit"].astype(str).str.upper() == unit_filter.upper())
    return float(bom_df.loc[mask, "Qty"].fillna(0).sum()) if "Qty" in bom_df.columns else 0.0


# -----------------------------------------------------------------
# Section 4A: Site Works BOM
# Qty from Page-4 bom_df for designed sites; capacity-based estimate
# for pending sites. Rate always from park["rate_sheet"].
# -----------------------------------------------------------------
cost_rows = []
for sid in all_site_ids:
    cfg         = site_config.get(sid, {})
    lyt         = site_layouts.get(sid, {})
    is_designed = sid in designed_ids
    bom_df_site = lyt.get("bom_df") if lyt else None
    metrics     = lyt.get("metrics", {}) if lyt else {}
    params      = lyt.get("params", {}) if lyt else {}

    ac_mw  = cfg.get("ac_mw", 0)
    dc_mwp = cfg.get("dc_mw", 0)
    dc_kwp = dc_mwp * 1000
    dc_wp  = dc_kwp * 1000   # Wp

    if is_designed and bom_df_site is not None and not bom_df_site.empty:
        # ── Use Page-4 BOMGenerator Qty × rate_sheet rates ─────────────────
        # BOMGenerator always has Rate=0 (user enters rates separately).
        # We read Qty from each BOM section and cost using rate_sheet.
        #
        # Actual BOM sections (from bom_generator.py):
        #   DC ARRAY SYSTEM   → modules (Nos), mounting structures (Sets)
        #   POWER CONVERSION  → inverters (Nos) with rating in description
        #   CABLES & WIRING   → all cables (km)
        #   AC SYSTEM         → transformer, HT panel
        #   EARTHING & PROTECTION → earthing, lightning arresters

        # Modules: Qty (Nos) × module_wp × rate_per_Wp
        n_modules  = _bom_section_qty(bom_df_site, ["SOLAR MODULE", "MODULE", "BIFACIAL", "PANEL"], unit_filter="Nos")
        mod_wp     = int(params.get("module_wp", 540)) if params else 540
        bom_dc_wp  = n_modules * mod_wp   # total Wp from actual BOM
        # Use bom DC Wp if available, else fall back to capacity DC Wp
        eff_dc_wp  = bom_dc_wp if bom_dc_wp > 0 else dc_wp
        cost_mod   = eff_dc_wp * rs["module_per_wp"]

        # Mounting: Qty (Sets) × modules_per_table × mod_wp × mounting_rate
        n_tables   = _bom_section_qty(bom_df_site, ["MOUNTING STRUCTURE", "MODULE MOUNTING STRUCTURE"])
        mpt        = int(params.get("modules_per_table", 30)) if params else 30
        cost_mount = (n_tables * mpt * mod_wp * rs["mounting_per_wp"]
                      if n_tables > 0 else eff_dc_wp * rs["mounting_per_wp"])

        # Inverters: extract rating×qty from each inverter row
        import re as _re
        cost_inv = 0.0
        for _, ir in bom_df_site.iterrows():
            sec  = str(ir.get("Section","")).upper()
            desc = str(ir.get("Description","")).upper()
            if "INVERTER" in sec or "POWER CONVERSION" in sec or "INVERTER" in desc:
                qty = float(ir.get("Qty", 0) or 0)
                m   = _re.search(r"(\d+(?:\.\d+)?)\s*KW", desc)
                rating_kw = float(m.group(1)) if m else (ac_mw * 1000 / max(qty, 1))
                cost_inv += qty * rating_kw * rs["inverter_per_kw"]
        if cost_inv == 0:
            cost_inv = ac_mw * 1000 * rs["inverter_per_kw"]

        # Cables: Qty (km) × 1000 × rate_per_m
        # BOMGenerator uses "CABLES & WIRING" section, description has cable type
        dc_cable_km = _bom_section_qty(bom_df_site,
            ["DC CABLE", "STRING CABLE", "MODULE CABLE", "4MM", "6MM"])
        ac_cable_km = _bom_section_qty(bom_df_site,
            ["AC CABLE", "LT CABLE", "HT CABLE", "POWER CABLE", "1.1KV"])
        all_cable_km = _bom_section_qty(bom_df_site, ["CABLE", "WIRING"], unit_filter="km")
        if dc_cable_km == 0 and ac_cable_km == 0 and all_cable_km > 0:
            dc_cable_km = all_cable_km * 0.6
            ac_cable_km = all_cable_km * 0.4
        cost_dc_cab = dc_cable_km * 1000 * rs["dc_cable_per_m"]
        cost_ac_cab = ac_cable_km * 1000 * rs["ac_cable_per_m"]

        # Civil + Electrical: capacity-based (BOM doesn't itemize per-MW civil)
        cost_civil  = ac_mw * rs["civil_per_mw"] * 1e5
        cost_elec   = ac_mw * rs["site_electrical_per_mw"] * 1e5

        source = "📋 Page-4 BOM"
    else:
        # ── Capacity-based estimate for pending sites ───────────
        cost_mod    = dc_wp  * rs['module_per_wp']
        cost_inv    = ac_mw  * 1000 * rs['inverter_per_kw']
        cost_dc_cab = (metrics.get("total_dc_km", 0) * 1000 * rs['dc_cable_per_m']
                       if metrics else 0)
        cost_ac_cab = (metrics.get("total_ac_km", 0) * 1000 * rs['ac_cable_per_m']
                       if metrics else 0)
        cost_mount  = dc_wp  * rs['mounting_per_wp']
        cost_civil  = ac_mw  * rs['civil_per_mw'] * 1e5
        cost_elec   = ac_mw  * rs['site_electrical_per_mw'] * 1e5
        source = "⚡ Estimate"

    # Land area from site boundary polygon
    _site_area_acres = 0.0
    try:
        if sites_gdf is not None:
            _srow = sites_gdf[sites_gdf["site_id"] == sid]
            if not _srow.empty:
                import geopandas as _gpd_la
                _site_area_acres = float(
                    _gpd_la.GeoDataFrame(geometry=[_srow.iloc[0].geometry], crs="EPSG:4326")
                    .to_crs(epsg=32644).geometry.iloc[0].area
                ) / 4046.856
    except Exception:
        pass
    if _site_area_acres == 0:
        _site_area_acres = dc_mwp * 4.0   # fallback ~4 acres/MW DC

    cost_land      = _site_area_acres * rs['land_per_acre'] * 1e5
    cost_approvals = rs['approvals_lump'] * 1e5

    site_total = cost_mod + cost_inv + cost_dc_cab + cost_ac_cab + cost_mount + cost_civil + cost_elec + cost_land + cost_approvals

    cost_rows.append({
        "Site ID":           sid,
        "Source":            source,
        "DC (kWp)":          round(dc_kwp, 1),
        "Modules (₹L)":      round(cost_mod        / 1e5, 2),
        "Inverters (₹L)":    round(cost_inv        / 1e5, 2),
        "DC Cable (₹L)":     round(cost_dc_cab     / 1e5, 2),
        "AC Cable (₹L)":     round(cost_ac_cab     / 1e5, 2),
        "Mounting (₹L)":     round(cost_mount      / 1e5, 2),
        "Civil (₹L)":        round(cost_civil      / 1e5, 2),
        "Electrical (₹L)":   round(cost_elec       / 1e5, 2),
        "Land (₹L)":         round(cost_land       / 1e5, 2),
        "Approvals (₹L)":    round(cost_approvals  / 1e5, 2),
        "Site Total (₹L)":   round(site_total      / 1e5, 2),
        "₹/Wp":              round(site_total / dc_wp, 2) if dc_wp else 0,
    })

cost_df = pd.DataFrame(cost_rows)

# Site-level totals row
numeric_cols = [
    "Modules (₹L)", "Inverters (₹L)", "DC Cable (₹L)", "AC Cable (₹L)",
    "Mounting (₹L)", "Civil (₹L)", "Electrical (₹L)",
    "Land (₹L)", "Approvals (₹L)", "Site Total (₹L)"
]
totals = {col: cost_df[col].sum() for col in numeric_cols}
totals["Site ID"]  = "SITES TOTAL"
totals["Source"]   = "—"
totals["DC (kWp)"] = cost_df["DC (kWp)"].sum()
totals["₹/Wp"]     = round(
    cost_df["Site Total (₹L)"].sum() * 1e5 / (cost_df["DC (kWp)"].sum() * 1000)
    if cost_df["DC (kWp)"].sum() else 0, 2
)
display_cost_df = pd.concat([cost_df, pd.DataFrame([totals])], ignore_index=True)

st.subheader("4A — Site Works BOM")
bom_source_counts = cost_df["Source"].value_counts().to_dict()
if "📋 Page-4 BOM" in bom_source_counts:
    st.caption(
        f"📋 {bom_source_counts.get('📋 Page-4 BOM', 0)} site(s) — quantities from Page-4 BOMGenerator. "
        f"⚡ {bom_source_counts.get('⚡ Estimate', 0)} site(s) — capacity-based estimate (Page-4 not yet run)."
    )
else:
    st.caption("⚡ All sites showing capacity-based estimates. Run Page-4 layout to get detailed BOM quantities.")

st.dataframe(display_cost_df, use_container_width=True, hide_index=True)
st.caption("ℹ️ ₹L = Indian Rupees Lakhs. Source column shows data origin per site.")

# =============================================================
# PARK INFRASTRUCTURE BOM
# =============================================================
st.subheader("4B — Park Common Infrastructure BOM")

roads_km      = park_summary.get("roads_total_km", 0)
drainage_km   = park_summary.get("drainage_total_km", 0)
culvert_count = park_summary.get("culvert_count", 0)
oh_km         = park_summary.get("oh_lines_total_km", 0)
pole_count    = park_summary.get("pole_count", 0)
spy_count     = park_summary.get("site_pole_yards", 0)

# Compute costs — internal infrastructure
c_road          = roads_km      * rs['road_per_km']        * 1e5
c_drain         = drainage_km   * rs['drainage_per_km']    * 1e5
c_culvert       = culvert_count * rs['culvert_each']
c_oh_line       = oh_km         * rs['oh_line_per_km']     * 1e5
c_pole          = pole_count    * rs['pole_each']
c_spy           = spy_count     * rs['site_pole_yard']
c_switchyard    = rs['switchyard_lump'] * 1e5
c_gate          = rs['gate_lump']       * 1e5
# Fencing costs now geometry-derived (rm × rate)
c_compound_wall = _compound_wall_m   * rs['compound_wall_rm']
c_chainlink     = _chainlink_fence_m * rs['chainlink_rm']
# ROW (external corridor for evacuation line)
c_row_land      = evac_line_km  * rs['row_land_per_km']    * 1e5
c_row_clear     = evac_line_km  * rs['row_clear_per_km']   * 1e5
# Evacuation line
c_evac_line     = evac_line_km  * rs['evac_line_per_km']   * 1e5
c_pooling_ss    = rs['pooling_ss_lump'] * 1e5 if evac_scenario.startswith("110kV") else 0.0

park_infra_rows = [
    # ── Civil ────────────────────────────────────────────────
    {
        "Item":        "Internal Roads",
        "Description": "Spine + access + site spurs",
        "Unit": "km",  "Qty": round(roads_km, 3),
        "Rate (₹)":    f"{rs['road_per_km']} L/km",
        "Amount (₹L)": round(c_road / 1e5, 2),
    },
    {
        "Item":        "Drainage Channels",
        "Description": "Parallel to site spurs",
        "Unit": "km",  "Qty": round(drainage_km, 3),
        "Rate (₹)":    f"{rs['drainage_per_km']} L/km",
        "Amount (₹L)": round(c_drain / 1e5, 2),
    },
    {
        "Item":        "Culverts / Road Crossings",
        "Description": "Drainage-road intersections",
        "Unit": "nos", "Qty": culvert_count,
        "Rate (₹)":    f"₹{int(rs['culvert_each']):,} each",
        "Amount (₹L)": round(c_culvert / 1e5, 2),
    },
    {
        "Item":        "Main Gate",
        "Description": "Park entry gate structure",
        "Unit": "LS",  "Qty": 1,
        "Rate (₹)":    f"₹{rs['gate_lump']} L LS",
        "Amount (₹L)": round(c_gate / 1e5, 2),
    },
    {
        "Item":        "Compound Wall (Precast Slabs)",
        "Description": "Park outer boundary wall",
        "Unit": "rm",  "Qty": round(_compound_wall_m, 0),
        "Rate (₹)":    f"₹{rs['compound_wall_rm']}/rm",
        "Amount (₹L)": round(_compound_wall_m * rs['compound_wall_rm'] / 1e5, 2),
    },
    {
        "Item":        "Chainlink Fence (Internal)",
        "Description": "Site boundary divisions inside park",
        "Unit": "rm",  "Qty": round(_chainlink_fence_m, 0),
        "Rate (₹)":    f"₹{rs['chainlink_rm']}/rm",
        "Amount (₹L)": round(_chainlink_fence_m * rs['chainlink_rm'] / 1e5, 2),
    },
    # ── Internal Electrical ───────────────────────────────────
    {
        "Item":        "33kV Internal OH Line",
        "Description": "Spine line + droppers (inside park)",
        "Unit": "km",  "Qty": round(oh_km, 3),
        "Rate (₹)":    f"{rs['oh_line_per_km']} L/km",
        "Amount (₹L)": round(c_oh_line / 1e5, 2),
    },
    {
        "Item":        "33kV Poles (Internal)",
        "Description": "Along internal OH line at 50m",
        "Unit": "nos", "Qty": pole_count,
        "Rate (₹)":    f"₹{int(rs['pole_each']):,} each",
        "Amount (₹L)": round(c_pole / 1e5, 2),
    },
    {
        "Item":        "Site Pole Yards (4-Pole Sub)",
        "Description": "One per site at spur entry",
        "Unit": "nos", "Qty": spy_count,
        "Rate (₹)":    f"₹{int(rs['site_pole_yard']):,} each",
        "Amount (₹L)": round(c_spy / 1e5, 2),
    },
    {
        "Item":        "33kV Switchyard / Pooling Station",
        "Description": "Internal pooling at spine start",
        "Unit": "LS",  "Qty": 1,
        "Rate (₹)":    f"{rs['switchyard_lump']} L LS",
        "Amount (₹L)": round(c_switchyard / 1e5, 2),
    },
    # ── Evacuation Infrastructure ─────────────────────────────
    {
        "Item":        f"Evacuation OH Line ({evac_voltage_label})",
        "Description": f"{evac_scenario} — park to destination SS",
        "Unit": "km",  "Qty": round(evac_line_km, 3),
        "Rate (₹)":    f"{rs['evac_line_per_km']} L/km",
        "Amount (₹L)": round(c_evac_line / 1e5, 2),
    },
] + ([
    {
        "Item":        "110kV Pooling Station",
        "Description": "Step-up from 33kV to 110kV for evacuation",
        "Unit": "LS",  "Qty": 1,
        "Rate (₹)":    f"{rs['pooling_ss_lump']} L LS",
        "Amount (₹L)": round(c_pooling_ss / 1e5, 2),
    },
] if evac_scenario.startswith("110kV") else []) + [
    # ── ROW ──────────────────────────────────────────────────
    {
        "Item":        "ROW — Land Acquisition",
        "Description": f"Corridor for {evac_voltage_label} evacuation line",
        "Unit": "km",  "Qty": round(evac_line_km, 3),
        "Rate (₹)":    f"{rs['row_land_per_km']} L/km",
        "Amount (₹L)": round(c_row_land / 1e5, 2),
    },
    {
        "Item":        "ROW — Clearance / Tree Cutting",
        "Description": "Along evacuation line corridor",
        "Unit": "km",  "Qty": round(evac_line_km, 3),
        "Rate (₹)":    f"{rs['row_clear_per_km']} L/km",
        "Amount (₹L)": round(c_row_clear / 1e5, 2),
    },
]

park_infra_df   = pd.DataFrame(park_infra_rows)
park_infra_total = park_infra_df["Amount (₹L)"].sum()

# Add total row
infra_total_row  = pd.DataFrame([{
    "Item": "PARK INFRA TOTAL", "Description": "", "Unit": "",
    "Qty": "", "Rate (₹)": "", "Amount (₹L)": round(park_infra_total, 2)
}])
st.dataframe(
    pd.concat([park_infra_df, infra_total_row], ignore_index=True),
    use_container_width=True, hide_index=True
)

# =============================================================
# SECTION 4C — DEVELOPER / EPC / CONTINGENCY MARGINS
# =============================================================
st.subheader("4C — Developer, EPC & Contingency")
st.caption("Enter margins as ₹ Lakhs per MW DC. Applied on top of total project cost (4A + 4B).")

_mc1, _mc2, _mc3 = st.columns(3)
with _mc1:
    dev_margin_per_mwdc = st.number_input(
        "Developer Margin (₹ Lakh/MW DC)",
        value=float(rs.get('dev_margin_per_mwdc', 10.0)),
        step=1.0, min_value=0.0, key="dev_margin"
    )
    rs['dev_margin_per_mwdc'] = dev_margin_per_mwdc
with _mc2:
    epc_margin_per_mwdc = st.number_input(
        "EPC Margin (₹ Lakh/MW DC)",
        value=float(rs.get('epc_margin_per_mwdc', 15.0)),
        step=1.0, min_value=0.0, key="epc_margin"
    )
    rs['epc_margin_per_mwdc'] = epc_margin_per_mwdc
with _mc3:
    contingency_per_mwdc = st.number_input(
        "Miscellaneous / Contingency (₹ Lakh/MW DC)",
        value=float(rs.get('contingency_per_mwdc', 5.0)),
        step=1.0, min_value=0.0, key="contingency"
    )
    rs['contingency_per_mwdc'] = contingency_per_mwdc

cost_dev         = total_dc_mw * dev_margin_per_mwdc
cost_epc         = total_dc_mw * epc_margin_per_mwdc
cost_contingency = total_dc_mw * contingency_per_mwdc
cost_4c_total    = cost_dev + cost_epc + cost_contingency

_4c_rows = [
    {"Item": "Developer Margin",           "Basis": f"₹{dev_margin_per_mwdc} L/MW DC × {total_dc_mw:.2f} MW DC",  "Amount (₹L)": round(cost_dev, 2)},
    {"Item": "EPC Margin",                 "Basis": f"₹{epc_margin_per_mwdc} L/MW DC × {total_dc_mw:.2f} MW DC",  "Amount (₹L)": round(cost_epc, 2)},
    {"Item": "Miscellaneous / Contingency","Basis": f"₹{contingency_per_mwdc} L/MW DC × {total_dc_mw:.2f} MW DC", "Amount (₹L)": round(cost_contingency, 2)},
    {"Item": "4C TOTAL",                   "Basis": "",                                                              "Amount (₹L)": round(cost_4c_total, 2)},
]
st.dataframe(pd.DataFrame(_4c_rows), use_container_width=True, hide_index=True)

# =============================================================
# GRAND TOTAL (4A + 4B + 4C)
# =============================================================
park_total_lakh  = cost_df["Site Total (₹L)"].sum()
park_infra_lakh  = round(park_infra_total, 2)
margins_lakh     = round(cost_4c_total, 2)
grand_total_lakh = round(park_total_lakh + park_infra_lakh + margins_lakh, 2)

st.markdown("---")
c1, c2, c3, c4 = st.columns(4)
c1.metric("4A Site Works",         f"₹ {park_total_lakh:.2f} L")
c2.metric("4B Park Infrastructure",f"₹ {park_infra_lakh:.2f} L")
c3.metric("4C Margins",            f"₹ {margins_lakh:.2f} L")
c4.metric("Grand Total (Est.)",    f"₹ {grand_total_lakh:.2f} L",
          delta=f"₹ {grand_total_lakh/total_dc_mw:.2f} L/MW DC" if total_dc_mw else None)

if grand_total_lakh > 0 and total_dc_mw > 0:
    site_pct   = round(park_total_lakh  / grand_total_lakh * 100, 1)
    infra_pct  = round(park_infra_lakh  / grand_total_lakh * 100, 1)
    margin_pct = round(margins_lakh     / grand_total_lakh * 100, 1)
    st.caption(
        f"4A Site works: **{site_pct}%** | "
        f"4B Park infra: **{infra_pct}%** | "
        f"4C Margins: **{margin_pct}%** | "
        f"Effective: **₹ {grand_total_lakh/total_dc_mw:.2f} L/MW DC**"
    )

# =============================================================
# SECTION 5 — CONSOLIDATED PARK MAP
# =============================================================
st.markdown("---")
st.header("🗺️ Consolidated Park Map")

elec = electrical
fac  = facilities
rds  = roads

# ── Layer Controls (matches screenshot) ───────────────────────
st.subheader("🗂️ Map Layer Controls")
lc1, lc2, lc3, lc4, lc5 = st.columns(5)
show_sites    = lc1.checkbox("Sites",       value=True)
show_roads    = lc2.checkbox("Roads",       value=True)
show_elec     = lc3.checkbox("Electrical",  value=True)
show_fac      = lc4.checkbox("Facilities",  value=True)
show_fencing  = lc5.checkbox("Fencing",     value=True)

with st.expander("🔧 Advanced Layer Control"):
    adv1, adv2, adv3, adv4 = st.columns(4)
    with adv1:
        st.markdown("**Sites**")
        show_designed = st.checkbox("Designed Sites",   value=True)
        show_pending  = st.checkbox("Pending Sites",    value=True)
    with adv2:
        st.markdown("**Roads**")
        show_main_access = st.checkbox("Main Access Road", value=True)
        show_spine       = st.checkbox("Spine Road",       value=True)
        show_spurs       = st.checkbox("Site Spurs",       value=True)
    with adv3:
        st.markdown("**Electrical**")
        show_oh_line    = st.checkbox("33kV OH Line",   value=True)
        show_evac_line  = st.checkbox("Evacuation Line",value=True)
        show_dp_yards   = st.checkbox("DP Yard",        value=True)
        show_switchyard = st.checkbox("Switchyard",     value=True)
    with adv4:
        st.markdown("**Facilities & Fencing**")
        show_gate         = st.checkbox("Main Gate",       value=True)
        show_evac_ss      = st.checkbox("Evacuation SS",   value=True)
        show_compound     = st.checkbox("Compound Wall",   value=True)
        show_chainlink    = st.checkbox("Chainlink Fence", value=True)

# Store layer selection for KMZ export (used in Section 6)
_layer_sel = {
    "sites": show_sites, "designed": show_designed, "pending": show_pending,
    "roads": show_roads, "main_access": show_main_access,
    "spine": show_spine, "spurs": show_spurs,
    "elec": show_elec, "oh_line": show_oh_line,
    "evac_line": show_evac_line, "dp_yards": show_dp_yards,
    "switchyard": show_switchyard,
    "fac": show_fac, "gate": show_gate, "evac_ss": show_evac_ss,
    "fencing": show_fencing, "compound": show_compound,
    "chainlink": show_chainlink,
}

try:
    cx = boundary.centroid.x
    cy = boundary.centroid.y

    m = folium.Map(location=[cy, cx], zoom_start=15, max_zoom=24, control_scale=True)

    # Park boundary
    folium.Polygon(
        locations=[(lat, lon) for lon, lat in boundary.exterior.coords],
        color="black", weight=2, fill=False, tooltip="Park Boundary"
    ).add_to(m)

    # Sites — colour coded by status
    COLOURS = ["#2ecc71","#3498db","#e67e22","#9b59b6",
               "#1abc9c","#e74c3c","#f1c40f","#34495e"]

    if show_sites:
      for idx, row in sites_gdf.iterrows():
        sid    = row["site_id"]
        cfg_r  = site_config.get(sid, {})
        colour = COLOURS[idx % len(COLOURS)]
        is_des = sid in designed_ids
        status = "\u2705 Designed" if is_des else "\u23f3 Pending"
        if is_des and not show_designed: continue
        if not is_des and not show_pending: continue
        geom   = row.geometry
        if geom is None or geom.is_empty: continue
        parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        largest = max(parts, key=lambda g: g.area)
        for part in parts:
            folium.Polygon(
                locations=[(lat, lon) for lon, lat in part.exterior.coords],
                color=colour, weight=2, fill=True, fill_color=colour,
                fill_opacity=0.35 if is_des else 0.15,
                tooltip=f"{sid} | {status}",
                popup=folium.Popup(
                    f"<div style='font-family:monospace;font-size:13px'>"
                    f"<b style='color:{colour}'>{sid}</b><br>"
                    f"Status: {status}<br>"
                    f"Area: <b>{cfg_r.get('area_acres',0):.2f} ac</b><br>"
                    f"AC: <b>{cfg_r.get('ac_mw',0):.2f} MW</b><br>"
                    f"DC: <b>{cfg_r.get('dc_mw',0):.2f} MW</b></div>",
                    max_width=200)
            ).add_to(m)
        cen = largest.centroid
        status_icon = "\u2705" if is_des else "\u23f3"
        folium.Marker(
            location=[cen.y, cen.x],
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:10px;font-weight:bold;color:white;'
                    f'background:{colour};border-radius:4px;padding:2px 5px;'
                    f'white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.4);'
                    f'text-align:center;line-height:1.4;">'
                    f'{sid} {status_icon}<br>'
                    f'<span style="font-weight:normal;font-size:9px">'
                    f'{cfg_r.get("ac_mw",0):.2f} MW</span></div>'
                ),
                icon_size=(100, 32), icon_anchor=(50, 16)
            )
        ).add_to(m)

        # ── Table polygons from Page-4 layout ─────────────────────────────
        if show_sites and sid in designed_ids:
            lyt = site_layouts.get(sid, {})
            layout_wgs = lyt.get("layout_wgs") if lyt else None

            if layout_wgs is not None and not getattr(layout_wgs, "empty", True):
                try:
                    _utm_epsg = park.get("utm_epsg", 32644)
                    import geopandas as _gpd_tbl

                    # Reproject site boundary to UTM for accurate metric clipping
                    _site_utm = _gpd_tbl.GeoDataFrame(
                        geometry=[row.geometry], crs="EPSG:4326"
                    ).to_crs(epsg=_utm_epsg).geometry.iloc[0]
                    # Small negative buffer (-0.1 m) to avoid boundary-edge artefacts
                    _site_utm_safe = _site_utm.buffer(-0.1)

                    # Reproject layout tables to UTM
                    _layout_utm = layout_wgs.to_crs(epsg=_utm_epsg)

                    for _, trow in _layout_utm.iterrows():
                        tgeom = trow.geometry
                        if tgeom is None or tgeom.is_empty:
                            continue

                        # Clip in UTM — clean metric geometry, no degree distortion
                        try:
                            clipped_utm = tgeom.intersection(_site_utm_safe)
                        except Exception:
                            clipped_utm = tgeom

                        if clipped_utm is None or clipped_utm.is_empty:
                            continue

                        # Keep only the largest piece if multi-part result
                        if clipped_utm.geom_type == "MultiPolygon":
                            clipped_utm = max(clipped_utm.geoms, key=lambda g: g.area)
                        if clipped_utm.geom_type != "Polygon":
                            continue

                        # Reproject clipped polygon back to WGS84 for folium
                        clipped_wgs = _gpd_tbl.GeoDataFrame(
                            geometry=[clipped_utm], crs=f"EPSG:{_utm_epsg}"
                        ).to_crs(epsg=4326).geometry.iloc[0]

                        folium.Polygon(
                            locations=[(lat, lon) for lon, lat in clipped_wgs.exterior.coords],
                            color=colour,
                            fill=True,
                            fill_color=colour,
                            fill_opacity=0.35,
                            weight=0.4,
                            tooltip=f"{sid} — Table"
                        ).add_to(m)

                except Exception as _tbl_err:
                    logger.warning(f"Table rendering failed for {sid}: {_tbl_err}")

    # Roads — respect layer controls
    if show_roads and show_main_access and "main_access" in roads and roads["main_access"] is not None:
        for geom in roads["main_access"]:
            folium.PolyLine([(lat,lon) for lon,lat in geom.coords],
                            color="#FF8C00", weight=4, tooltip="Main Access Road").add_to(m)
    if show_roads and show_spine and "spine" in roads and roads["spine"] is not None:
        for geom in roads["spine"]:
            folium.PolyLine([(lat,lon) for lon,lat in geom.coords],
                            color="#444444", weight=3, tooltip="Spine Road").add_to(m)
    if show_roads and show_spurs and "site_spurs" in roads and roads["site_spurs"] is not None:
        for geom in roads["site_spurs"]:
            folium.PolyLine([(lat,lon) for lon,lat in geom.coords],
                            color="#888888", weight=2, tooltip="Site Spur").add_to(m)

    # Electrical
    elec = electrical
    if show_elec and show_oh_line and "oh_line_33kv" in elec and elec["oh_line_33kv"] is not None:
        for geom in elec["oh_line_33kv"]:
            folium.PolyLine([(lat,lon) for lon,lat in geom.coords],
                            color="#CC5500", weight=3, tooltip="33kV OH Line").add_to(m)

    # ── Droppers: 33kV line → each site DP yard ──────────────────────────
    # Prefer Page-4 DP yard positions; fall back to Page-2 pole yards.
    if show_elec and show_dp_yards:
        _oh_line = None
        if "oh_line_33kv" in elec and elec["oh_line_33kv"] is not None:
            _oh_list = list(elec["oh_line_33kv"])
            if _oh_list:
                _oh_line = _oh_list[0]   # main 33kV line geometry (WGS84)

        # Collect DP yard WGS84 positions — snap inside site boundary if needed
        _dp_positions = {}
        for _sid, _lyt in site_layouts.items():
            _dp = (_lyt.get("dp_yard") or {}) if _lyt else {}
            if not (_dp.get("lat") and _dp.get("lon")):
                continue
            _dp_lat, _dp_lon = _dp["lat"], _dp["lon"]
            try:
                from shapely.geometry import Point as _Pt_dp
                if sites_gdf is not None:
                    _sr = sites_gdf[sites_gdf["site_id"] == _sid]
                    if not _sr.empty and not _sr.iloc[0].geometry.contains(_Pt_dp(_dp_lon, _dp_lat)):
                        import geopandas as _gpd_dp
                        _UTM = park.get("utm_epsg", 32644)
                        _bnd_utm = _gpd_dp.GeoDataFrame(
                            geometry=[_sr.iloc[0].geometry], crs="EPSG:4326"
                        ).to_crs(epsg=_UTM).geometry.iloc[0]
                        _dp_utm = _gpd_dp.GeoDataFrame(
                            geometry=[_Pt_dp(_dp_lon, _dp_lat)], crs="EPSG:4326"
                        ).to_crs(epsg=_UTM).geometry.iloc[0]
                        _near = _bnd_utm.boundary.interpolate(_bnd_utm.boundary.project(_dp_utm))
                        _cx, _cy = _bnd_utm.centroid.x, _bnd_utm.centroid.y
                        _dx, _dy = _cx - _near.x, _cy - _near.y
                        _d = (_dx**2 + _dy**2) ** 0.5
                        if _d > 0:
                            from shapely.geometry import Point as _Pt_s
                            _snapped_ll = _gpd_dp.GeoDataFrame(
                                geometry=[_Pt_s(_near.x + _dx/_d*10, _near.y + _dy/_d*10)],
                                crs=f"EPSG:{_UTM}"
                            ).to_crs(epsg=4326).geometry.iloc[0]
                            _dp_lat, _dp_lon = float(_snapped_ll.y), float(_snapped_ll.x)
            except Exception:
                pass
            _dp_positions[_sid] = (_dp_lat, _dp_lon)

        # Fallback: Page-2 pole yards for sites without Page-4 DP
        if "site_pole_yards" in elec and elec["site_pole_yards"] is not None:
            _spy_list  = list(elec["site_pole_yards"])
            _site_ids  = list(sites_gdf["site_id"]) if sites_gdf is not None else []
            for _i, _spy in enumerate(_spy_list):
                _sid = _site_ids[_i] if _i < len(_site_ids) else f"SITE_{_i+1:02d}"
                if _sid not in _dp_positions:
                    _dp_positions[_sid] = (float(_spy.y), float(_spy.x))

        # Draw dropper from nearest point on 33kV line to each DP yard
        for _sid, (_dp_lat, _dp_lon) in _dp_positions.items():
            if _oh_line is not None:
                try:
                    from shapely.geometry import Point as _Pt
                    # Project DP yard onto OH line (both in WGS84)
                    _dp_pt    = _Pt(_dp_lon, _dp_lat)
                    _proj_pt  = _oh_line.interpolate(_oh_line.project(_dp_pt))
                    folium.PolyLine(
                        [(_proj_pt.y, _proj_pt.x), (_dp_lat, _dp_lon)],
                        color="red", weight=2, dash_array="4,4",
                        tooltip=f"33kV Dropper → {_sid}"
                    ).add_to(m)
                except Exception:
                    pass
            # DP yard marker
            folium.RegularPolygonMarker(
                location=(_dp_lat, _dp_lon),
                number_of_sides=4, radius=7,
                color="#8B4513", fill=True,
                fill_color="#FFA500", fill_opacity=0.9,
                tooltip=f"DP Yard — {_sid}"
            ).add_to(m)

    if show_elec and show_evac_line and "evacuation_line" in elec and elec["evacuation_line"] is not None:
        for geom in elec["evacuation_line"]:
            folium.PolyLine([(lat,lon) for lon,lat in geom.coords],
                            color="#8B0000", weight=4, tooltip="Evacuation Line").add_to(m)
    if show_fac and show_switchyard and fac.get("switchyard"):
        g = fac["switchyard"]
        folium.Marker([g.y, g.x],
            icon=folium.Icon(color="purple", icon="bolt", prefix="fa"),
            tooltip="\u26a1 Switchyard / 33kV Pooling").add_to(m)
    if show_fac and show_gate and fac.get("main_gate"):
        g = fac["main_gate"]
        folium.Marker([g.y, g.x],
            icon=folium.Icon(color="orange", icon="road", prefix="fa"),
            tooltip="\U0001f6a7 Main Gate").add_to(m)

    # Compound Wall — park outer boundary (thick dark line)
    if show_fencing and show_compound and boundary is not None:
        _bnd_coords = [(lat, lon) for lon, lat in boundary.exterior.coords]
        folium.PolyLine(
            _bnd_coords,
            color="#1a1a1a", weight=4, opacity=0.85,
            tooltip=f"Compound Wall (Precast) — {_compound_wall_m:.0f} rm"
        ).add_to(m)

    # Chainlink Fence — each site's full boundary minus the park outer wall
    from shapely.ops import unary_union

    if show_fencing and show_chainlink and sites_gdf is not None and not sites_gdf.empty:
        try:
            _utm_epsg  = park.get("utm_epsg", 32644)
            _sites_utm = sites_gdf.to_crs(epsg=_utm_epsg)
            _park_bnd_utm = None
            if boundary is not None:
                import geopandas as _gpd_cl
                _park_bnd_utm = _gpd_cl.GeoDataFrame(
                    geometry=[boundary], crs="EPSG:4326"
                ).to_crs(epsg=_utm_epsg).geometry.iloc[0]

            chainlink_segs = []
            for _geom in _sites_utm.geometry:
                if _geom is None or _geom.is_empty:
                    continue
                _ring = _geom.boundary
                if _park_bnd_utm is not None:
                    try:
                        _ring = _ring.difference(_park_bnd_utm.boundary.buffer(0.5))
                    except Exception:
                        pass
                if _ring is not None and not _ring.is_empty:
                    chainlink_segs.append(_ring)

            if chainlink_segs:
                _merged_wgs = gpd.GeoSeries(
                    [unary_union(chainlink_segs)], crs=_utm_epsg
                ).to_crs(epsg=4326).iloc[0]

                def _draw_cl(geom):
                    if geom is None or geom.is_empty:
                        return
                    if geom.geom_type == "LineString":
                        folium.PolyLine(
                            [(lat, lon) for lon, lat in geom.coords],
                            color="#444444", weight=2, dash_array="6,6",
                            tooltip="Chainlink Fence (Internal)"
                        ).add_to(m)
                    elif geom.geom_type in ("MultiLineString", "GeometryCollection"):
                        for g in geom.geoms:
                            _draw_cl(g)
                    elif geom.geom_type == "Polygon":
                        _draw_cl(geom.exterior)

                _draw_cl(_merged_wgs)

        except Exception as _e:
            logger.warning(f"Chainlink fence drawing failed: {_e}")

    # Facilities
    fac = facilities
    _edest_lat = pooling_ss_lat if evac_scenario.startswith("110kV") else evac_ss_lat
    _edest_lon = pooling_ss_lon if evac_scenario.startswith("110kV") else evac_ss_lon
    if show_fac and show_evac_ss and _edest_lat and _edest_lon and (_edest_lat != 0.0 or _edest_lon != 0.0):
        folium.Marker(
            location=[_edest_lat, _edest_lon],
            icon=folium.Icon(color="red", icon="bolt", prefix="fa"),
            tooltip="\U0001f3ed Evacuation Substation"
        ).add_to(m)
        if fac.get("switchyard"):
            _sw = fac["switchyard"]
            folium.PolyLine(
                [(_sw.y, _sw.x), (_edest_lat, _edest_lon)],
                color="#8B0000", weight=3, dash_array="10,5",
                tooltip=f"Evacuation Line ({evac_line_km:.2f} km)"
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, height=600, width=None, returned_objects=[])

    # ── Streamlit legend below map ────────────────────────
    st.markdown("**🗺️ Map Legend**")
    _lg1, _lg2, _lg3, _lg4 = st.columns(4)
    with _lg1:
        st.markdown(
            "**Sites**\n\n"
            "🟩 Designed site (solid fill)\n\n"
            "⬜ Pending site (faint fill)\n\n"
            "**Fencing**\n\n"
            "⬛ Compound Wall (precast) — outer boundary\n\n"
            "▪️ Chainlink Fence — internal site divisions (dashed)"
        )
    with _lg2:
        st.markdown(
            "**Roads**\n\n"
            "🟠 Main Access Road\n\n"
            "⬛ Spine Road\n\n"
            "🩶 Site Spurs"
        )
    with _lg3:
        st.markdown(
            "**Electrical**\n\n"
            "🟫 33kV Internal OH Line\n\n"
            "🔴 Evacuation OH Line (dashed)\n\n"
            "🟧 DP Yard / 4-Pole Substation\n\n"
            "💜 Switchyard / 33kV Pooling Station"
        )
    with _lg4:
        st.markdown(
            "**Facilities**\n\n"
            "🟠 Main Gate\n\n"
            "🔴 Evacuation Substation / 110kV Pooling SS\n\n"
            "_(Hover any element on the map for details)_"
        )


except Exception as _map_err:
    st.error(f"Map could not render: {_map_err}")
    import traceback
    st.code(traceback.format_exc())

# =============================================================
# SECTION 6 — CONSOLIDATED KMZ EXPORT
# =============================================================
st.markdown("---")
st.header("⬇️ Export")

col_kmz, col_xlsx, col_pdf = st.columns(3)

with col_kmz:
    st.subheader("🌍 KMZ Export")
    if st.button("Generate Consolidated KMZ", use_container_width=True):
        try:
            import simplekml
            kml = simplekml.Kml(name="SURYA Park")

            # Park boundary folder
            bnd_fol = kml.newfolder(name="Park Boundary")
            pol = bnd_fol.newpolygon(name="Park Boundary")
            pol.outerboundaryis = list(boundary.exterior.coords)
            pol.style.polystyle.color = simplekml.Color.changealphaint(30, simplekml.Color.blue)
            pol.style.linestyle.color = simplekml.Color.black
            pol.style.linestyle.width = 2

            # Sites folder — respects layer selection
            if _layer_sel.get("sites"):
              sites_fol = kml.newfolder(name="Sites")
              for idx, row in sites_gdf.iterrows():
                sid  = row["site_id"]
                is_des = sid in designed_ids
                if is_des and not _layer_sel.get("designed"): continue
                if not is_des and not _layer_sel.get("pending"): continue
                cfg_r = site_config.get(sid, {})
                geom = row.geometry
                if geom is None or geom.is_empty: continue
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
                sp = sites_fol.newpolygon(name=sid)
                sp.outerboundaryis = list(geom.exterior.coords)
                status = "Designed" if is_des else "Pending"
                sp.style.polystyle.color = simplekml.Color.changealphaint(
                    80, simplekml.Color.green if is_des else simplekml.Color.yellow)
                sp.style.linestyle.color = simplekml.Color.darkgreen
                sp.style.linestyle.width = 2
                sp.description = (
                    f"<b>{sid}</b><br>Status: {status}<br>"
                    f"Area: {cfg_r.get('area_acres',0):.2f} ac<br>"
                    f"AC: {cfg_r.get('ac_mw',0):.2f} MW<br>"
                    f"DC: {cfg_r.get('dc_mw',0):.2f} MW"
                )

            # Roads folder — respects layer selection
            if _layer_sel.get("roads"):
              road_fol = kml.newfolder(name="Roads")
              for rtype, color, label, sel_key in [
                ("spine",       simplekml.Color.gray,      "Spine Road",     "spine"),
                ("site_spurs",  simplekml.Color.lightgray, "Site Spur",      "spurs"),
                ("main_access", simplekml.Color.orange,    "Main Access",    "main_access"),
              ]:
                if not _layer_sel.get(sel_key): continue
                _rgeoms = roads.get(rtype)
                if _rgeoms is not None:
                    for geom in _rgeoms:
                        ls = road_fol.newlinestring(name=label, coords=list(geom.coords))
                        ls.style.linestyle.color = color
                        ls.style.linestyle.width = 2

            # Electrical folder — respects layer selection
            if _layer_sel.get("elec"):
              elec_fol = kml.newfolder(name="Electrical")
              if _layer_sel.get("oh_line") and "oh_line_33kv" in electrical and electrical["oh_line_33kv"] is not None:
                for geom in electrical["oh_line_33kv"]:
                    ls = elec_fol.newlinestring(name="33kV OH Line", coords=list(geom.coords))
                    ls.style.linestyle.color = simplekml.Color.orange
                    ls.style.linestyle.width = 3
              if _layer_sel.get("dp_yards") and "site_pole_yards" in electrical and electrical["site_pole_yards"] is not None:
                for _i, _dp in enumerate(electrical["site_pole_yards"]):
                    pt = elec_fol.newpoint(name=f"DP Yard {_i+1}", coords=[(_dp.x, _dp.y)])
                    pt.style.iconstyle.color = simplekml.Color.orange
              if _layer_sel.get("switchyard") and facilities.get("switchyard"):
                _sw = facilities["switchyard"]
                pt = elec_fol.newpoint(name="Switchyard", coords=[(_sw.x, _sw.y)])
                pt.style.iconstyle.color = simplekml.Color.purple

            kmz_buf = BytesIO()
            kml.savekmz(kmz_buf)
            kmz_buf.seek(0)

            st.download_button(
                "⬇️ Download Park KMZ",
                kmz_buf,
                file_name="SURYA_park_consolidated.kmz",
                mime="application/vnd.google-earth.kmz"
            )
            st.success("✅ KMZ ready")
        except Exception as e:
            st.error(f"KMZ generation failed: {e}")

# =============================================================
# EXCEL EXPORT
# =============================================================
with col_xlsx:
    st.subheader("📊 Excel Report")
    if st.button("Generate Excel Report", use_container_width=True):
        try:
            import openpyxl
            from openpyxl.styles import (
                Font, PatternFill, Alignment, Border, Side
            )
            from openpyxl.utils import get_column_letter

            wb = openpyxl.Workbook()

            # Styles
            HDR_FILL  = PatternFill("solid", fgColor="1B4F72")
            HDR_FONT  = Font(bold=True, color="FFFFFF", size=11)
            TOTL_FILL = PatternFill("solid", fgColor="D6EAF8")
            TOTL_FONT = Font(bold=True, size=11)
            TITLE_FONT = Font(bold=True, size=14, color="1B4F72")
            thin = Side(style="thin")
            bdr  = Border(left=thin, right=thin, top=thin, bottom=thin)

            def write_header(ws, row, cols):
                for c, val in enumerate(cols, 1):
                    cell = ws.cell(row=row, column=c, value=val)
                    cell.fill = HDR_FILL
                    cell.font = HDR_FONT
                    cell.alignment = Alignment(horizontal="center", wrap_text=True)
                    cell.border = bdr

            def write_row(ws, row, vals, bold=False, fill=None):
                for c, val in enumerate(vals, 1):
                    cell = ws.cell(row=row, column=c, value=val)
                    if bold:
                        cell.font = TOTL_FONT
                    if fill:
                        cell.fill = fill
                    cell.border = bdr
                    cell.alignment = Alignment(horizontal="center")

            def autofit(ws):
                for col in ws.columns:
                    max_len = max((len(str(cell.value or "")) for cell in col), default=8)
                    ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 30)

            # ── Sheet 1: Park Summary ──────────────────────────
            ws1 = wb.active
            ws1.title = "Park Summary"
            ws1["A1"] = "SURYA – Solar Park Master Consolidation Report"
            ws1["A1"].font = TITLE_FONT
            ws1["A2"] = f"Generated: {datetime.datetime.now().strftime('%d %b %Y %H:%M')}"
            ws1.merge_cells("A1:F1")

            hdr = ["Parameter", "Value"]
            write_header(ws1, 4, hdr)
            summary_data = [
                ("Total Sites",          total_sites),
                ("Sites Designed",       designed_count),
                ("Sites Pending",        pending_count),
                ("Total AC Capacity",    f"{total_ac_mw:.2f} MW"),
                ("Total DC Capacity",    f"{total_dc_mw:.2f} MW"),
                ("Avg DC/AC Ratio",      f"{total_dc_mw/total_ac_mw:.2f}" if total_ac_mw else "—"),
                ("Total Area",           f"{total_acres:.2f} acres"),
                ("Roads Total",          f"{park_summary.get('roads_total_km',0):.2f} km"),
                ("OH Lines Total",       f"{park_summary.get('oh_lines_total_km',0):.2f} km"),
                ("Report Currency",      "INR (₹ Lakhs)"),
            ]
            for i, (k, v) in enumerate(summary_data, 5):
                write_row(ws1, i, [k, v])
            autofit(ws1)

            # ── Sheet 2: Site Summary ──────────────────────────
            ws2 = wb.create_sheet("Site Summary")
            cols2 = ["Site ID","Status","Area (ac)","AC (MW)","DC (MW)",
                     "DC/AC","Tables","Inverters","DC Cable (km)","AC Cable (km)","Placement"]
            write_header(ws2, 1, cols2)
            for i, r in enumerate(rows, 2):
                write_row(ws2, i, [r[c] for c in cols2])
            autofit(ws2)

            # ── Sheet 3: Cost Estimation ───────────────────────
            ws3 = wb.create_sheet("Cost Estimation")
            cols3 = ["Site ID","Status","DC (kWp)","Modules (₹L)","Inverters (₹L)",
                     "DC Cable (₹L)","AC Cable (₹L)","Mounting (₹L)",
                     "Civil (₹L)","Electrical (₹L)","Site Total (₹L)","₹/Wp"]
            write_header(ws3, 1, cols3)
            for i, r in enumerate(cost_rows, 2):
                write_row(ws3, i, [r[c] for c in cols3])
            # Totals row
            tot_vals = [
                "PARK TOTAL", "—",
                round(cost_df["DC (kWp)"].sum(),1),
                round(cost_df["Modules (₹L)"].sum(),2),
                round(cost_df["Inverters (₹L)"].sum(),2),
                round(cost_df["DC Cable (₹L)"].sum(),2),
                round(cost_df["AC Cable (₹L)"].sum(),2),
                round(cost_df["Mounting (₹L)"].sum(),2),
                round(cost_df["Civil (₹L)"].sum(),2),
                round(cost_df["Electrical (₹L)"].sum(),2),
                round(grand_total_lakh, 2),
                round(totals["₹/Wp"],2)
            ]
            write_row(ws3, len(cost_rows)+2, tot_vals, bold=True, fill=TOTL_FILL)

            # 4C — margins sheet
            ws3c = wb.create_sheet("4C Margins")
            write_header(ws3c, 1, ["Item", "Basis", "Amount (₹L)"])
            for i, r in enumerate(_4c_rows, 2):
                write_row(ws3c, i, [r["Item"], r["Basis"], r["Amount (₹L)"]],
                          bold=(r["Item"].endswith("TOTAL")),
                          fill=TOTL_FILL if r["Item"].endswith("TOTAL") else None)
            autofit(ws3c)

            # Grand total summary sheet row
            ws1.append([])
            ws1.append(["Grand Total (4A+4B+4C)", f"₹ {grand_total_lakh:.2f} Lakhs"])
            ws1.append(["Effective Cost", f"₹ {grand_total_lakh/total_dc_mw:.2f} L/MW DC" if total_dc_mw else "—"])
            autofit(ws3)

            # ── Sheet 4: Park Infrastructure BOM ──────────────
            ws4 = wb.create_sheet("Park Infra BOM")
            cols4 = ["Item","Description","Unit","Qty","Rate (₹)","Amount (₹L)"]
            write_header(ws4, 1, cols4)
            for i, r in enumerate(park_infra_rows, 2):
                write_row(ws4, i, [r[c] for c in cols4])
            # total row
            write_row(ws4, len(park_infra_rows)+2,
                      ["PARK INFRA TOTAL","","","","", round(park_infra_total,2)],
                      bold=True, fill=TOTL_FILL)
            autofit(ws4)

            # ── Sheet 5: Unit Rates ────────────────────────────
            ws5 = wb.create_sheet("Unit Rates")
            write_header(ws5, 1, ["Item", "Rate", "Unit"])
            rate_data = [
                ("Solar Module",                        rs['module_per_wp'],       "₹/Wp"),
                ("Inverter",                            rs['inverter_per_kw'],     "₹/kW AC"),
                ("DC Cable",                            rs['dc_cable_per_m'],      "₹/m"),
                ("AC Cable",                            rs['ac_cable_per_m'],      "₹/m"),
                ("Mounting Structure",                  rs['mounting_per_wp'],     "₹/Wp"),
                ("Civil Works",                         rs['civil_per_mw'],        "₹ Lakh/MW AC"),
                ("Site Electrical",                     rs['site_electrical_per_mw'],   "₹ Lakh/MW AC"),
                ("Internal Road",                       rs['road_per_km'],         "₹ Lakh/km"),
                ("Drainage Channel",                    rs['drainage_per_km'],     "₹ Lakh/km"),
                ("Culvert",                             rs['culvert_each'],        "₹ each"),
                ("33kV Internal OH Line",               rs['oh_line_per_km'],      "₹ Lakh/km"),
                ("33kV Pole",                           rs['pole_each'],           "₹ each"),
                ("Site Pole Yard",                      rs['site_pole_yard'],      "₹ each"),
                ("33kV Switchyard (LS)",                rs['switchyard_lump'],     "₹ Lakh LS"),
                ("Main Gate (LS)",                      rs['gate_lump'],           "₹ Lakh LS"),
                ("Compound Wall (LS)",                  rate_compound_wall_lump,  "₹ Lakh LS"),
                (f"Evacuation OH Line {evac_voltage_label}", rs['evac_line_per_km'], "₹ Lakh/km"),
                ("110kV Pooling SS (LS)",               rs['pooling_ss_lump'],     "₹ Lakh LS"),
                ("ROW Land Acquisition",                rs['row_land_per_km'],     "₹ Lakh/km"),
                ("ROW Clearance / Tree Cutting",        rs['row_clear_per_km'],    "₹ Lakh/km"),
                ("Evacuation Scenario",                 evac_scenario,            "—"),
                ("Evacuation Line Length",              f"{evac_line_km:.3f} km", "km"),
            ]
            for i, (item, rate, unit) in enumerate(rate_data, 2):
                write_row(ws5, i, [item, rate, unit])
            autofit(ws5)

            xlsx_buf = BytesIO()
            wb.save(xlsx_buf)
            xlsx_buf.seek(0)

            st.download_button(
                "⬇️ Download Excel Report",
                xlsx_buf,
                file_name="SURYA_consolidation_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            st.success("✅ Excel ready (5 sheets)")
        except Exception as e:
            st.error(f"Excel generation failed: {e}")
            import traceback
            st.code(traceback.format_exc())

# =============================================================
# PDF EXPORT
# =============================================================
with col_pdf:
    st.subheader("📄 PDF Report")
    if st.button("Generate PDF Report", use_container_width=True):
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import cm
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer,
                Table, TableStyle, PageBreak, HRFlowable
            )
            from reportlab.lib.enums import TA_CENTER, TA_LEFT

            pdf_buf = BytesIO()
            doc = SimpleDocTemplate(
                pdf_buf,
                pagesize=A4,
                rightMargin=1.5*cm, leftMargin=1.5*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            styles = getSampleStyleSheet()
            DARK_BLUE = colors.HexColor("#1B4F72")
            LIGHT_BLUE = colors.HexColor("#D6EAF8")
            MID_BLUE  = colors.HexColor("#2980B9")
            
            title_style = ParagraphStyle(
                "Title", parent=styles["Title"],
                fontSize=18, textColor=DARK_BLUE, spaceAfter=6
            )
            h2_style = ParagraphStyle(
                "H2", parent=styles["Heading2"],
                fontSize=13, textColor=DARK_BLUE, spaceBefore=12, spaceAfter=4
            )
            body_style = ParagraphStyle(
                "Body", parent=styles["Normal"],
                fontSize=9, spaceAfter=4
            )
            caption_style = ParagraphStyle(
                "Caption", parent=styles["Normal"],
                fontSize=8, textColor=colors.gray, spaceAfter=6
            )

            cover_title = ParagraphStyle(
                name='CoverTitle',
                fontSize=26,
                leading=32,
                alignment=TA_CENTER,
                spaceAfter=20
            )

            cover_subtitle = ParagraphStyle(
                name='CoverSubtitle',
                fontSize=16,
                leading=22,
                alignment=TA_CENTER,
                spaceAfter=10
            )

            cover_capacity = ParagraphStyle(
                name='CoverCapacity',
                fontSize=20,
                leading=26,
                alignment=TA_CENTER,
                textColor="#0B6E4F",
                spaceAfter=20
            )

            def make_table(data, col_widths=None):
                t = Table(data, colWidths=col_widths, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND",  (0,0), (-1,0), DARK_BLUE),
                    ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
                    ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
                    ("FONTSIZE",    (0,0), (-1,-1), 8),
                    ("ALIGN",       (0,0), (-1,-1), "CENTER"),
                    ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, LIGHT_BLUE]),
                    ("GRID",        (0,0), (-1,-1), 0.4, colors.grey),
                    ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
                    ("TOPPADDING",  (0,0), (-1,-1), 4),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 4),
                ]))
                return t

            story = []

            # -------------------------------
            # COVER PAGE
            # -------------------------------

            story.append(Spacer(1, 3*cm))

            story.append(Paragraph(
                "SURYA – Solar Park Master Consolidation",
                cover_title
            ))

            story.append(Spacer(1, 1*cm))

            # Optional image (if you have one)
            try:
                cover_img = RLImage("assets/solar_cover.jpg", width=18*cm, height=8*cm)
                story.append(cover_img)
                story.append(Spacer(1, 1*cm))
            except:
                pass  # safe fallback

            # Report date
            story.append(Paragraph(
                "Report Date: 24 Apr 2026",
                cover_subtitle
            ))

            # Capacity (highlighted)
            story.append(Paragraph(
                "<b>Total Capacity: 15.45 MW AC / 20.11 MW DC</b>",
                cover_capacity
            ))

            story.append(Spacer(1, 2*cm))

            story.append(Paragraph(
                "Generated by SURYA – Solar Park Master Planner",
                cover_subtitle
            ))

            # 🚨 IMPORTANT
            story.append(PageBreak())

            # Park summary table — build inline (avoids NameError from outer scope)
            _pdf_summary_data = [
                ("Total Sites",          total_sites),
                ("Sites Designed",       designed_count),
                ("Sites Pending",        pending_count),
                ("Total AC Capacity",    f"{total_ac_mw:.2f} MW"),
                ("Total DC Capacity",    f"{total_dc_mw:.2f} MW"),
                ("Avg DC/AC Ratio",      f"{total_dc_mw/total_ac_mw:.2f}" if total_ac_mw else "—"),
                ("Total Area",           f"{total_acres:.2f} acres"),
                ("Roads Total",          f"{park_summary.get('roads_total_km',0):.2f} km"),
                ("OH Lines Total",       f"{park_summary.get('oh_lines_total_km',0):.2f} km"),
                ("Compound Wall",        f"{_compound_wall_m:.0f} rm"),
                ("Chainlink Fence",      f"{_chainlink_fence_m:.0f} rm"),
                ("Evacuation Scenario",  evac_scenario),
                ("Evacuation Line",      f"{evac_line_km:.2f} km"),
                ("Grand Total (Est.)",   f"₹ {grand_total_lakh:.2f} Lakhs"),
            ]
            story.append(Paragraph("1. Park Summary", h2_style))
            sum_data = [["Parameter", "Value"]] + [
                [k, str(v)] for k, v in _pdf_summary_data
            ]
            story.append(make_table(sum_data, col_widths=[9*cm, 8*cm]))
            story.append(Spacer(1, 0.4*cm))

            # Site summary table
            story.append(Paragraph("2. Site-by-Site Summary", h2_style))
            s_cols = ["Site","Status","Area(ac)","AC(MW)","DC(MW)","DC/AC","Tables","Inv.","DC Cab(km)","AC Cab(km)"]
            s_data = [s_cols] + [
                [r["Site ID"], r["Status"], r["Area (ac)"], r["AC (MW)"],
                 r["DC (MW)"], r["DC/AC"], r["Tables"], r["Inverters"],
                 r["DC Cable (km)"], r["AC Cable (km)"]]
                for r in rows
            ]
            story.append(make_table(s_data))
            story.append(Spacer(1, 0.4*cm))
            story.append(PageBreak())

            # Cost table
            story.append(Paragraph("3. Cost Estimation (₹ Lakhs)", h2_style))
            c_cols = ["Site","DC(kWp)","Modules","Inverters","Cables","Mounting","Civil","Elec.","Total(₹L)","₹/Wp"]
            c_data = [c_cols] + [
                [r["Site ID"], r["DC (kWp)"],
                 r["Modules (₹L)"], r["Inverters (₹L)"],
                 round(r["DC Cable (₹L)"] + r["AC Cable (₹L)"], 2),
                 r["Mounting (₹L)"], r["Civil (₹L)"], r["Electrical (₹L)"],
                 r["Site Total (₹L)"], r["₹/Wp"]]
                for r in cost_rows
            ] + [["TOTAL", round(cost_df["DC (kWp)"].sum(),1),
                  round(cost_df["Modules (₹L)"].sum(),2),
                  round(cost_df["Inverters (₹L)"].sum(),2),
                  round((cost_df["DC Cable (₹L)"]+cost_df["AC Cable (₹L)"]).sum(),2),
                  round(cost_df["Mounting (₹L)"].sum(),2),
                  round(cost_df["Civil (₹L)"].sum(),2),
                  round(cost_df["Electrical (₹L)"].sum(),2),
                  round(grand_total_lakh,2), round(totals["₹/Wp"],2)]]
            story.append(make_table(c_data))
            story.append(Spacer(1, 0.2*cm))
            story.append(Paragraph(
                f"Sites Total: ₹{park_total_lakh:.2f}L",
                body_style
            ))
            story.append(Spacer(1, 0.4*cm))

            # Park infra BOM table
            story.append(Paragraph("4. Park Common Infrastructure BOM", h2_style))
            pi_cols = ["Item","Unit","Qty","Rate","Amount (₹L)"]
            pi_data = [pi_cols] + [
                [r["Item"], r["Unit"], r["Qty"], r["Rate (₹)"], r["Amount (₹L)"]]
                for r in park_infra_rows
            ] + [["PARK INFRA TOTAL","","","", round(park_infra_total,2)]]
            story.append(make_table(pi_data))
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph(
                f"Grand Total (Site Works ₹{park_total_lakh:.2f}L + "
                f"Park Infra ₹{park_infra_lakh:.2f}L): "
                f"<b>₹ {grand_total_lakh:.2f} Lakhs</b>",
                body_style
            ))
            story.append(Spacer(1, 0.4*cm))

            # 4C margins
            story.append(Paragraph("5. Developer, EPC & Contingency Margins", h2_style))
            _4c_pdf = [["Item", "Basis", "Amount (₹L)"]] + [
                [r["Item"], r["Basis"], r["Amount (₹L)"]] for r in _4c_rows
            ]
            story.append(make_table(_4c_pdf, col_widths=[6*cm, 7*cm, 4*cm]))
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph(
                f"<b>Grand Total (4A + 4B + 4C): ₹ {grand_total_lakh:.2f} Lakhs</b> "
                f"| ₹ {grand_total_lakh/total_dc_mw:.2f} L/MW DC",
                body_style
            ))
            story.append(Spacer(1, 0.4*cm))
          

            # Unit rates — defined inline (rate_data lives in Excel block scope)
            story.append(Paragraph("6. Unit Rates Applied", h2_style))
            _pdf_rate_data = [
                ("Solar Module",                         rs['module_per_wp'],        "₹/Wp"),
                ("Inverter",                             rs['inverter_per_kw'],      "₹/kW AC"),
                ("DC Cable",                             rs['dc_cable_per_m'],       "₹/m"),
                ("AC Cable",                             rs['ac_cable_per_m'],       "₹/m"),
                ("Mounting Structure",                   rs['mounting_per_wp'],      "₹/Wp"),
                ("Civil Works",                          rs['civil_per_mw'],         "₹ Lakh/MW AC"),
                ("Site Electrical",                      rs['site_electrical_per_mw'],    "₹ Lakh/MW AC"),
                ("Internal Road",                        rs['road_per_km'],          "₹ Lakh/km"),
                ("Drainage Channel",                     rs['drainage_per_km'],      "₹ Lakh/km"),
                ("Culvert",                              rs['culvert_each'],         "₹ each"),
                ("33kV Internal OH Line",                rs['oh_line_per_km'],       "₹ Lakh/km"),
                ("33kV Pole",                            rs['pole_each'],            "₹ each"),
                ("Site Pole Yard",                       rs['site_pole_yard'],       "₹ each"),
                ("33kV Switchyard (LS)",                 rs['switchyard_lump'],      "₹ Lakh LS"),
                ("Main Gate (LS)",                       rs['gate_lump'],            "₹ Lakh LS"),
                ("Compound Wall",                        rs['compound_wall_rm'],     "₹/rm"),
                ("Chainlink Fence",                      rs['chainlink_rm'],         "₹/rm"),
                (f"Evacuation OH Line {evac_voltage_label}", rs['evac_line_per_km'], "₹ Lakh/km"),
                ("110kV Pooling SS (LS)",                rs['pooling_ss_lump'],      "₹ Lakh LS"),
                ("ROW Land Acquisition",                 rs['row_land_per_km'],      "₹ Lakh/km"),
                ("ROW Clearance",                        rs['row_clear_per_km'],     "₹ Lakh/km"),
                ("Evacuation Scenario",                  evac_scenario,             "—"),
                ("Evacuation Line Length",               f"{evac_line_km:.3f} km",  "km"),
            ]
            ur_data = [["Item","Rate","Unit"]] + [[i, str(r), u] for i, r, u in _pdf_rate_data]
            story.append(make_table(ur_data, col_widths=[8*cm, 5*cm, 5*cm]))
            story.append(PageBreak())

            story.append(Spacer(1, 0.4*cm))
            story.append(HRFlowable(width="100%", color=colors.lightgrey))

            # ── Park Map (matplotlib static render) ───────────────────────

	    
            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                import matplotlib.patches as mpatches
                from matplotlib.patches import Polygon as MPoly
                from matplotlib.collections import PatchCollection
                from io import BytesIO as _BIO
                from reportlab.platypus import Image as RLImage

                fig, ax = plt.subplots(figsize=(16, 9))
                ax.set_aspect("equal")
                ax.set_facecolor("#f5f5f0")
                fig.patch.set_facecolor("white")

                COLOURS_PDF = [
                    "#2ecc71","#3498db","#e67e22","#9b59b6",
                    "#1abc9c","#e74c3c","#f1c40f","#34495e",
                ]

                # Park boundary
                if boundary_map is not None:
                    bx, by = boundary_map.exterior.xy
                    ax.fill(bx, by, alpha=0.08, color="black")
                    ax.plot(bx, by, color="black", linewidth=1.5, label="Park Boundary")

                # Sites + tables
                legend_patches = []
                for idx, row in sites_gdf.iterrows():
                    sid    = row["site_id"]
                    colour = COLOURS_PDF[idx % len(COLOURS_PDF)]
                    is_des = sid in designed_ids
                    geom   = row.geometry
                    if geom is None or geom.is_empty:
                        continue
                    if geom.geom_type == "MultiPolygon":
                        geom = max(geom.geoms, key=lambda g: g.area)
                    bx, by = geom.exterior.xy
                    ax.fill(bx, by, alpha=0.25, color=colour)
                    ax.plot(bx, by, color=colour, linewidth=1.0)
                    cen = geom.centroid
                    cfg_r = site_config.get(sid, {})
                    ax.annotate(
                        f"{sid}\n{cfg_r.get('ac_mw',0):.1f} MW",
                        xy=(cen.x, cen.y),
                        fontsize=6, ha="center", va="center",
                        color="white",
                        bbox=dict(boxstyle="round,pad=0.2", fc=colour, alpha=0.8, ec="none")
                    )
                    # Table polygons
                    if is_des:
                        lyt_map = site_layouts.get(sid, {})
                        lwgs    = lyt_map.get("layout_wgs") if lyt_map else None
                        if lwgs is not None and not getattr(lwgs, "empty", True):
                            for _, tr in lwgs.iterrows():
                                tg = tr.geometry
                                if tg is None or tg.is_empty:
                                    continue
                                if tg.geom_type == "MultiPolygon":
                                    tg = max(tg.geoms, key=lambda g: g.area)
                                tx, ty = tg.exterior.xy
                                ax.fill(tx, ty, alpha=0.55, color=colour)
                    legend_patches.append(
                        mpatches.Patch(color=colour,
                                       label=f"{sid} {'✓' if is_des else '⏳'}")
                    )

                # Roads — explicit None checks (GeoSeries is ambiguous with 'or')
                _spine_geoms = roads.get("spine")
                if _spine_geoms is not None:
                    for geom in _spine_geoms:
                        rx, ry = geom.xy
                        ax.plot(rx, ry, color="#444444", linewidth=2.0, label="Spine Road")
                _access_geoms = roads.get("main_access")
                if _access_geoms is not None:
                    for geom in _access_geoms:
                        rx, ry = geom.xy
                        ax.plot(rx, ry, color="#FF8C00", linewidth=2.5, label="Main Access")
                _spur_geoms = roads.get("site_spurs")
                if _spur_geoms is not None:
                    for geom in _spur_geoms:
                        rx, ry = geom.xy
                        ax.plot(rx, ry, color="#888888", linewidth=1.0)

                # 33kV OH Line + droppers
                elec_map = electrical or {}
                _oh_geoms = elec_map.get("oh_line_33kv")
                if _oh_geoms is not None:
                    for geom in _oh_geoms:
                        ex, ey = geom.xy
                        ax.plot(ex, ey, color="#CC5500", linewidth=1.5,
                                linestyle="--", label="33kV OH Line")
                _drop_geoms = elec_map.get("droppers")
                if _drop_geoms is not None:
                    for geom in _drop_geoms:
                        ex, ey = geom.xy
                        ax.plot(ex, ey, color="red", linewidth=0.8, linestyle=":")

                # Switchyard / Gate
                fac_map = facilities or {}
                if fac_map.get("switchyard"):
                    g = fac_map["switchyard"]
                    ax.plot(g.x, g.y, "v", color="purple", markersize=8,
                            label="Switchyard", zorder=5)
                if fac_map.get("main_gate"):
                    g = fac_map["main_gate"]
                    ax.plot(g.x, g.y, "^", color="darkorange", markersize=8,
                            label="Main Gate", zorder=5)

                ax.legend(
                    handles=legend_patches + ax.get_legend_handles_labels()[0],
                    loc="lower right", fontsize=6, framealpha=0.8,
                    ncol=min(4, len(legend_patches)+3)
                )
                ax.set_xlabel("Longitude", fontsize=7)
                ax.set_ylabel("Latitude", fontsize=7)
                ax.tick_params(labelsize=6)
                ax.set_title("SURYA – Consolidated Park Map", fontsize=10, fontweight="bold")
                plt.tight_layout(pad=0.5)

                _map_buf = _BIO()
                fig.savefig(_map_buf, format="png", dpi=150, bbox_inches="tight")
                _map_buf.seek(0)
                plt.close(fig)

                from reportlab.platypus import PageBreak

                story.append(PageBreak())

                story.append(Paragraph("6. Park Map", h2_style))
                story.append(Spacer(1, 0.3*cm))

                story.append(RLImage(_map_buf, width=17*cm, height=9.5*cm))

            except Exception as _map_pdf_err:
                story.append(Paragraph(
                    f"[Map could not be rendered: {_map_pdf_err}]", caption_style
                ))

            story.append(Spacer(1, 0.4*cm))
            story.append(HRFlowable(width="100%", color=colors.lightgrey))
            story.append(Paragraph(
                "Generated by SURYA – Solar Park Master Planner",
                caption_style
            ))

            doc.build(story)
            pdf_buf.seek(0)

            st.download_button(
                "⬇️ Download PDF Report",
                pdf_buf,
                file_name="SURYA_consolidation_report.pdf",
                mime="application/pdf"
            )
            st.success("✅ PDF ready (5 sections)")

        except ImportError:
            st.error("❌ reportlab not installed. Run: pip install reportlab")
        except Exception as e:
            st.error(f"PDF generation failed: {e}")
            import traceback
            st.code(traceback.format_exc())
