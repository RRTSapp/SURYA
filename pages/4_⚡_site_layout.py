#!/usr/bin/env python3
"""
Solar Layout & BOM Pro — Page 4
================================
UI layer only.  All generation business logic lives in:

    modules/core_engine.py  →  run_complete_layout_generation()

"""

import datetime
import logging

import geopandas as gpd
import pandas as pd
import streamlit as st
from pathlib import Path

# ---------------------------------------------------------------------------
# Core module imports
# ---------------------------------------------------------------------------
from modules.layout.boundary import calculate_utm_epsg, parse_boundary_file
from modules.layout_generator import LayoutGenerator

layout_gen = LayoutGenerator()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

st.set_page_config(layout="wide", page_title="Solar Layout & BOM Pro", page_icon="☀️")

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

st.title("☀️ Site Layout & BOM")



try:
    from modules.state_manager import StateManager
    from modules.ui_components import UIComponents
    from modules.bom_generator import BOMGenerator
    from modules.export_manager import ExportManager
    from modules.core_engine import run_complete_layout_generation, safe_len
    logger.info("✅ All modules imported successfully")
except ImportError as e:
    logger.error("❌ Module import failed: %s", e)
    st.error(f"Module import failed: {e}")
    st.stop()

# ---------------------------------------------------------------------------
# Park mode detection
# ---------------------------------------------------------------------------
park = st.session_state.get("park_data")

PARK_MODE = (
    park is not None
    and isinstance(park, dict)
    and park.get("sites") is not None
    and park.get("site_kmz_files") is not None
)

if PARK_MODE:
    logger.info("🏗️ Page-4 running in PARK MODE")
else:
    logger.info("🧩 Page-4 running in STANDALONE MODE")

if PARK_MODE and not park.get("sites_generated"):
    st.warning(
        "⚠️ **Complete Page-3 (Freeze Configuration) before designing layouts.**\n\n"
        "AC/DC capacity must be frozen before inverter sizing can begin."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Persistent session-state defaults
# ---------------------------------------------------------------------------
if "layout_generated" not in st.session_state:
    st.session_state["layout_generated"] = False
if "layout_metrics" not in st.session_state:
    st.session_state["layout_metrics"] = {}

# Fix st.rerun() compatibility with older Streamlit
if not hasattr(st, "rerun"):
    if hasattr(st, "experimental_rerun"):
        st.rerun = st.experimental_rerun
    else:
        def _rerun():
            raise st.script_runner.RerunException(
                st.script_request_queue.RerunData(None)
            )
        st.rerun = _rerun


# ---------------------------------------------------------------------------
# Utility helpers kept in Page-4 (UI formatting only)
# ---------------------------------------------------------------------------

def _fmt_km(val_km) -> str:
    try:
        return f"{val_km:,.3f} km"
    except Exception:
        return str(val_km)


def _fmt_m(val_m) -> str:
    try:
        return f"{val_m:,.1f} m"
    except Exception:
        return str(val_m)


# ---------------------------------------------------------------------------
# Page-4 cross-tab state bridge
# ---------------------------------------------------------------------------
def _page4_has_data(value):
    """Return True only when a state value contains usable data."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    try:
        return len(value) > 0
    except Exception:
        return True


def _resolve_page4_site_id(park, site_id=None):
    """Resolve the active Park-Mode site from every known Page-4/park alias."""
    if site_id:
        return site_id
    for key in ("selected_site_id", "page4_bridge_site_id", "active_site_id"):
        value = st.session_state.get(key)
        if value:
            return value
    for key in ("selected_site", "active_site_id", "selected_site_id"):
        value = park.get(key) if isinstance(park, dict) else None
        if value:
            return value
    layouts = park.get("site_layouts", {}) if isinstance(park, dict) else {}
    if isinstance(layouts, dict) and len(layouts) == 1:
        return next(iter(layouts))
    return None


def _normalize_page4_metrics(state: StateManager):
    """Normalize legacy/incomplete layout metrics for every Page-4 consumer.

    Older layout-generator metric payloads can omit placement_pct (and a few
    related display keys) when their internal metrics pass encounters a
    partially populated object.  The engineering layout itself may still be
    valid.  Derive the missing display metrics from the canonical layout and
    infrastructure state so dashboard/BOM/shadow/generation tabs cannot crash.
    """
    try:
        metrics = state.get_value("metrics", None) or st.session_state.get("layout_metrics", {}) or {}
        if not isinstance(metrics, dict):
            metrics = {}
        else:
            metrics = dict(metrics)

        layout = state.get_value("layout_utm", None)
        infra = state.get_value("infra_results", None) or st.session_state.get("infra_results_backup") or {}
        cable = state.get_value("cable_results", None) or st.session_state.get("cable_results_backup") or {}
        walkways = state.get_value("walkways", None)
        if walkways is None:
            walkways = st.session_state.get("walkways_backup", [])

        def _n(v):
            try:
                return len(v) if v is not None else 0
            except Exception:
                return 0

        if not metrics.get("tables"):
            metrics["tables"] = _n(layout)
        if not metrics.get("inverters"):
            metrics["inverters"] = _n(infra.get("inverter_points")) if isinstance(infra, dict) else 0
        if not metrics.get("scbs"):
            metrics["scbs"] = _n(infra.get("scb_points")) if isinstance(infra, dict) else 0
        if not metrics.get("dc_items"):
            metrics["dc_items"] = _n(infra.get("dc_items")) if isinstance(infra, dict) else 0
        if not metrics.get("ac_items"):
            metrics["ac_items"] = _n(infra.get("ac_items")) if isinstance(infra, dict) else 0
        if not metrics.get("cables"):
            metrics["cables"] = _n(cable.get("cables")) if isinstance(cable, dict) else 0
        if not metrics.get("walkways"):
            metrics["walkways"] = _n(walkways)
        metrics.setdefault("trenches", metrics.get("cables", 0))

        # Reconstruct core electrical/layout metrics when a saved payload
        # contains counts but lacks the fields expected by the Page-4 UI.
        params = state.get_value("params", None) or {}
        inverter_configs = state.get_value("inverter_configs", None) or []
        try:
            modules_per_string = int(params.get("modules_per_string", 0) or 0)
            strings_per_table = int(params.get("strings_per_table", 0) or 0)
            modules_per_table = modules_per_string * strings_per_table
            if not metrics.get("modules_per_table") and modules_per_table:
                metrics["modules_per_table"] = modules_per_table
            if not metrics.get("total_modules"):
                metrics["total_modules"] = int(metrics.get("tables", 0) or 0) * modules_per_table
            module_wp = float(params.get("module_wp", 0) or 0)
            if not metrics.get("dc_kwp") and metrics.get("total_modules") and module_wp:
                metrics["dc_kwp"] = float(metrics["total_modules"]) * module_wp / 1000.0
            if not metrics.get("ac_kw"):
                ac_kw = 0.0
                for cfg in inverter_configs:
                    try:
                        if hasattr(cfg, "total_capacity_kw"):
                            ac_kw += float(cfg.total_capacity_kw)
                        elif isinstance(cfg, dict):
                            qty = float(cfg.get("quantity", cfg.get("qty", 1)) or 0)
                            rating = float(cfg.get("rating_kw", cfg.get("rating", cfg.get("capacity_kw", 0))) or 0)
                            ac_kw += qty * rating
                    except Exception:
                        continue
                if ac_kw:
                    metrics["ac_kw"] = ac_kw
            if not metrics.get("dc_ac_ratio") and metrics.get("ac_kw"):
                metrics["dc_ac_ratio"] = float(metrics.get("dc_kwp", 0) or 0) / float(metrics["ac_kw"])
        except Exception as exc:
            logger.warning("Page-4 core metric reconstruction warning: %s", exc)

        # Placement quality is based on the actual average routed DC distance
        # when available; otherwise retain a meaningful neutral label.
        avg_dist = float(metrics.get("avg_scb_dist", 0.0) or 0.0)
        if not metrics.get("placement_quality") or metrics.get("placement_quality") == "Unknown":
            if avg_dist <= 0:
                metrics["placement_quality"] = "Unknown"
            elif avg_dist < 50:
                metrics["placement_quality"] = "🟢 Excellent"
            elif avg_dist < 80:
                metrics["placement_quality"] = "🟡 Good"
            else:
                metrics["placement_quality"] = "🟠 Fair"

        # Placement percentage = actual inverters / expected inverters.
        # If no expected count is supplied, actual placement is 100%.
        if metrics.get("placement_pct") is None:
            actual = int(metrics.get("inverters", 0) or 0)
            expected = int(st.session_state.get("expected_inverters", 0) or 0)
            if expected <= 0:
                expected = actual
            metrics["placement_pct"] = round((actual / expected) * 100.0, 1) if expected else 100.0

        # Keep legacy dashboard fields present even when the generator's
        # optional metrics stage failed.
        for key, default in {
            "total_dc_km": 0.0, "total_ac_km": 0.0,
            "string_length": 0.0, "scb_feeder_length": 0.0,
            "string_inv_ac": 0.0, "central_inv_ac": 0.0,
            "acdb_tf": 0.0, "ht_cable": 0.0,
            "avg_scb_dist": avg_dist,
        }.items():
            metrics.setdefault(key, default)

        state.set_value("metrics", metrics)
        st.session_state["layout_metrics"] = metrics
        return metrics
    except Exception as exc:
        logger.warning("Page-4 metrics normalization warning: %s", exc)
        return st.session_state.get("layout_metrics", {}) or {}


def _sync_page4_state_bridge(state: StateManager, site_id=None):
    """
    Single canonical bridge used by ALL six Page-4 tabs.

    State precedence:
      1. live StateManager data when it is usable;
      2. session-state redundant backups;
      3. the currently selected/saved Park-Mode site.

    Empty default containers ({} / []) are treated as missing.  This is
    important because StateManager initializes infra_results/cable_results as
    empty dictionaries, which otherwise prevents recovery from succeeding.
    """
    try:
        park = st.session_state.get("park_data") or {}
        selected = _resolve_page4_site_id(park, site_id)
        layouts = park.get("site_layouts", {}) if isinstance(park, dict) else {}
        saved = layouts.get(selected) if selected and isinstance(layouts, dict) else None

        primary_layout = state.get_value("layout_utm", None)
        primary_infra = state.get_value("infra_results", None)
        primary_cable = state.get_value("cable_results", None)
        primary_walk = state.get_value("walkways", None)

        # 1) Recover independently from session-state backups.  Never let an
        # empty StateManager default ({}) hide a valid backup.
        if not _page4_has_data(primary_infra):
            backup = st.session_state.get("infra_results_backup")
            if isinstance(backup, dict) and backup:
                state.set_value("infra_results", backup)
                primary_infra = backup
        if not _page4_has_data(primary_cable):
            backup = st.session_state.get("cable_results_backup")
            if isinstance(backup, dict) and backup:
                state.set_value("cable_results", backup)
                primary_cable = backup
        if not _page4_has_data(primary_walk):
            backup = st.session_state.get("walkways_backup")
            if backup is not None:
                state.set_value("walkways", backup)
                primary_walk = backup

        # 2) Recover from the saved Park-Mode layout.  A saved layout is a
        # valid source whether or not the 'locked' marker is present; older
        # park_adapter saves did not write that marker.
        if isinstance(saved, dict):
            saved_layout = saved.get("layout_utm")
            saved_infra = saved.get("infra_results")
            saved_cable = saved.get("cable_results")
            saved_walk = saved.get("walkways")

            if not _page4_has_data(primary_layout) and _page4_has_data(saved_layout):
                for key in (
                    "layout_utm", "layout_wgs", "params", "inverter_configs",
                    "metrics", "utm_epsg",
                ):
                    if saved.get(key) is not None:
                        state.set_value(key, saved.get(key))
                primary_layout = saved_layout

            if not _page4_has_data(primary_infra) and isinstance(saved_infra, dict) and saved_infra:
                state.set_value("infra_results", saved_infra)
                primary_infra = saved_infra
            if not _page4_has_data(primary_cable) and isinstance(saved_cable, dict) and saved_cable:
                state.set_value("cable_results", saved_cable)
                primary_cable = saved_cable
            if not _page4_has_data(primary_walk) and saved_walk is not None:
                state.set_value("walkways", saved_walk)
                primary_walk = saved_walk

            saved_metrics = saved.get("metrics")
            if not _page4_has_data(state.get_value("metrics", None)) and saved_metrics:
                state.set_value("metrics", saved_metrics)

        # 3) Derive readiness from actual data rather than stale boolean flags.
        layout_ready = _page4_has_data(state.get_value("layout_utm", None))
        infra_ready = (
            isinstance(state.get_value("infra_results", None), dict)
            and bool(state.get_value("infra_results", None))
            and isinstance(state.get_value("cable_results", None), dict)
            and bool(state.get_value("cable_results", None))
        )
        if infra_ready:
            state.set_value("infra_generated", True)
        elif state.get_value("infra_generated", False) and not _page4_has_data(state.get_value("infra_results", None)):
            state.set_value("infra_generated", False)

        # Normalize once all recovery sources have had a chance to populate the
        # canonical state.  This prevents legacy dashboard consumers from
        # failing on missing placement_pct/placement_quality.
        _normalize_page4_metrics(state)

        # 4) Mirror canonical values to direct session-state aliases used by
        # BOM, Shadow, Generation, BESS and visualization modules.
        mirror_keys = (
            "boundary_ll", "boundary_utm", "layout_utm", "layout_wgs",
            "utm_epsg", "infra_results", "cable_results", "walkways",
            "params", "inverter_configs", "metrics",
        )
        for key in mirror_keys:
            value = state.get_value(key, None)
            if _page4_has_data(value):
                st.session_state[key] = value

        metrics = state.get_value("metrics", None)
        if _page4_has_data(metrics):
            st.session_state["layout_metrics"] = metrics

        infra = state.get_value("infra_results", None)
        cable = state.get_value("cable_results", None)
        walkways = state.get_value("walkways", None)
        if isinstance(infra, dict) and infra:
            st.session_state["infra_results_backup"] = infra
        if isinstance(cable, dict) and cable:
            st.session_state["cable_results_backup"] = cable
            _cables = cable.get("cables")
            if _cables is not None:
                st.session_state["cables_gdf"] = _cables
        if walkways is not None:
            st.session_state["walkways_backup"] = walkways

        if layout_ready:
            st.session_state["layout_generated"] = True
        if infra_ready:
            st.session_state["infra_generated_backup"] = True
            st.session_state["layout_generated"] = True

        if selected:
            st.session_state["selected_site_id"] = selected
            st.session_state["page4_bridge_site_id"] = selected

        return infra_ready
    except Exception as exc:
        logger.exception("Page-4 state bridge warning: %s", exc)
        return bool(st.session_state.get("layout_generated", False))


# ---------------------------------------------------------------------------
# Persistent layout panel  (shown at top of Tab 1 after generation)
# ---------------------------------------------------------------------------

def render_persistent_layout_panel():
    if not st.session_state.get("layout_generated"):
        return

    m      = st.session_state.get("layout_metrics", {}) or {}
    infra  = st.session_state.get("infra_results", {}) or {}
    metrics = dict(m)

    defaults = {
        "string_length": 0.0, "scb_feeder_length": 0.0,
        "string_inv_ac": 0.0, "central_inv_ac": 0.0,
        "acdb_tf": 0.0, "ht_cable": 0.0,
        "total_dc_km": 0.0, "total_ac_km": 0.0,
        "avg_scb_dist": 0.0, "tables": 0, "scbs": 0, "inverters": 0,
        "dc_items": 0, "ac_items": 0, "cables": 0, "trenches": 0, "walkways": 0,
    }
    for k, v in defaults.items():
        metrics.setdefault(k, v)

    placement_quality = m.get("placement_quality", "🟠 Fair")
    tables   = metrics["tables"]
    scbs     = safe_len(infra.get("scb_points"))
    inverters = safe_len(infra.get("inverter_points"))

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap');
    html, body, [class*="css"] { font-family: 'Poppins', sans-serif !important; font-weight: 400 !important; color: #2d3648 !important; }
    h3, h4 { font-weight: 500 !important; color: #1e293b !important; margin-bottom: 4px !important; }
    .clean-card { padding: 14px 18px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; margin-bottom: 10px; }
    .section-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b; margin-bottom: 8px; }
    </style>
    """, unsafe_allow_html=True)

    st.success("✅ Layout generated and persisted")

    def card(label, value, icon=""):
        return f"""<div class='clean-card'><div class='section-title'>{icon} {label}</div><div style='font-size:1.4rem;font-weight:600'>{value}</div></div>"""

    def placement_badge(q):
        color = {"🟢": "#dcfce7", "🟡": "#fef9c3", "🟠": "#ffedd5"}.get(q[:2], "#f1f5f9")
        return f"<span style='background:{color};padding:4px 12px;border-radius:20px;font-size:0.85rem'>{q}</span>"

    left, right = st.columns([3, 1])

    with left:
        r1c1, r1c2, r1c3 = st.columns(3)
        r1c1.markdown(card("Tables", tables, "🔲"), unsafe_allow_html=True)
        r1c2.markdown(card("SCBs", scbs, "🔵"), unsafe_allow_html=True)
        r1c3.markdown(card("Inverters", inverters, "⚡"), unsafe_allow_html=True)

        r2c1, r2c2 = st.columns(2)
        r2c1.markdown(card("Total DC", _fmt_km(metrics["total_dc_km"]), "🔵"), unsafe_allow_html=True)
        r2c2.markdown(card("Total AC", _fmt_km(metrics["total_ac_km"]), "🟠"), unsafe_allow_html=True)

        st.markdown("<h4 class='section-title' style='font-size:17px;'>📈 Placement Quality</h4>", unsafe_allow_html=True)
        st.markdown(placement_badge(placement_quality), unsafe_allow_html=True)
        st.progress(int(metrics.get("placement_pct", 0)))

    with right:
        st.markdown("<h3 class='section-title'>🕒 Last Generated</h3>", unsafe_allow_html=True)
        st.caption(m.get("last_generated_at", "N/A"))
        if st.button("Dismiss"):
            st.session_state["layout_generated"] = False
            st.session_state["layout_metrics"] = {}
            st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# render_automatic_placement  — UI only
# ---------------------------------------------------------------------------

def render_automatic_placement(state, layout_gen, params, inverter_configs):
    """Render the automatic placement button and progress; delegate to engine."""
    from modules.state.park_adapter import save_site_layout_to_park

    st.success("""
    🤖 **Automatic Placement Mode Active**

    The system will automatically:
    - ✅ Optimise SCB locations using K-means clustering
    - ✅ Place inverters at optimal centroid
    - ✅ Position DC side (Control Room) near inverters
    - ✅ Position AC side (Transformer + VCB) near grid connection
    - ✅ Place gate at AC side boundary
    - ✅ Place borewell at opposite corner
    - ✅ Generate cable trenches for excavation
    - ✅ Create walkways for O&M access

    No zone drawing required! Just click **Generate Complete Layout** below.
    """)

    if not st.button(
        "🤖 Generate Complete Layout (Automatic)",
        type="primary",
        width="stretch",
    ):
        return

    progress_bar = st.progress(0)
    status_text  = st.empty()

    def _progress(step: int, total: int, msg: str):
        progress_bar.progress(int(step / total * 100) / 100)
        status_text.text(msg)

    results = run_complete_layout_generation(
        state=state,
        layout_gen=layout_gen,
        params=params,
        inverter_configs=inverter_configs,
        progress_callback=_progress,
        expected_inverters=st.session_state.get("expected_inverters", 0),
    )

    if results["error"]:
        st.error(f"❌ Layout generation failed: {results['error']}")
        with st.expander("🔍 Technical Details"):
            st.code(results.get("traceback", ""))
        with st.expander("🔧 Recovery Options"):
            if hasattr(state, "recover_infrastructure") and st.button("🔄 Attempt Data Recovery"):
                infra, cable, walk = state.recover_infrastructure()
                if infra:
                    st.success("✅ Some data recovered! Check the map below.")
                    st.rerun()
                else:
                    st.error("❌ No recoverable data found")
        return

    # ── Write session_state backups (for map recovery) ────────────────────
    infra_results  = results["infra_results"]
    cable_results  = results["cable_results"]
    walkways       = results["walkways"]
    metrics_mapped = dict(results.get("metrics") or {})
    # Normalize the engine payload before any dashboard or Park persistence
    # consumes it.  The engine can legitimately finish layout generation even
    # when its optional metrics aggregation encounters a partial object.
    state.set_value("metrics", metrics_mapped)
    metrics_mapped = _normalize_page4_metrics(state)
    cable_summary  = results["cable_summary"]
    inverter_summary = results["inverter_summary"]

    st.session_state["infra_results_backup"]  = infra_results
    st.session_state["cable_results_backup"]  = cable_results
    st.session_state["walkways_backup"]       = walkways
    st.session_state["cable_summary"]         = cable_summary
    st.session_state["inverter_summary"]      = inverter_summary
    st.session_state["layout_generated"]      = True
    st.session_state["layout_metrics"]        = metrics_mapped

    progress_bar.progress(1.0)
    status_text.text("✅ Layout generation complete!")

    # ── Success display ───────────────────────────────────────────────────
    st.success("✅ Layout generation complete – all data persisted!")
    st.markdown("---")
    st.subheader("🎯 Generation Summary")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Tables Generated",  metrics_mapped["tables"])
        st.metric("SCBs Placed",       metrics_mapped["scbs"])
        st.metric("Inverters Placed",  metrics_mapped["inverters"])
    with col2:
        st.metric("DC Infrastructure", metrics_mapped["dc_items"])
        st.metric("AC Infrastructure", metrics_mapped["ac_items"])
        st.metric("Cable Routes",      metrics_mapped["cables"])
    with col3:
        st.metric("Walkways",          metrics_mapped["walkways"])
        st.metric("Placement Quality", metrics_mapped["placement_quality"])
        st.metric("Success Rate",      f"{metrics_mapped['placement_pct']}%")

    with st.expander("🔍 Data Persistence Status", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.write("**Primary Storage:**")
            st.write("✅ infra_results"  if state.get_value("infra_results")  else "❌ infra_results")
            st.write("✅ cable_results"  if state.get_value("cable_results")  else "❌ cable_results")
            st.write("✅ walkways"       if state.get_value("walkways")       else "❌ walkways")
        with col2:
            st.write("**Backup Storage:**")
            st.write("✅ infra_backup"   if "infra_results_backup"  in st.session_state else "❌ infra_backup")
            st.write("✅ cable_backup"   if "cable_results_backup"  in st.session_state else "❌ cable_backup")
            st.write("✅ walkways_backup" if "walkways_backup"      in st.session_state else "❌ walkways_backup")
        if hasattr(state, "verify_infrastructure_integrity"):
            st.json(state.verify_infrastructure_integrity())

    st.info("✅ Scroll down to see the updated interactive map with all infrastructure!")

    # ── Park mode save ────────────────────────────────────────────────────
    selected_site_id = st.session_state.get("selected_site_id")
    if PARK_MODE and selected_site_id:
        _save_to_park(state, selected_site_id, metrics_mapped)

    # Generation occurs inside the Layout fragment. A fragment rerun does not
    # rerender sibling fragments, so BOM/Shadow/Generation/BESS can otherwise
    # remain on their pre-generation state. Force one app-level rerun after the
    # completed layout has been persisted; the locked path then restores data
    # without regenerating the layout.
    st.rerun()


def _save_to_park(state, selected_site_id: str, metrics_mapped: dict):
    """Persist the generated site layout into park_data."""
    from modules.state.park_adapter import save_site_layout_to_park
    try:
        bom_df = BOMGenerator().generate_comprehensive_bom(state)
        save_site_layout_to_park(
            park=st.session_state.park_data,
            site_id=selected_site_id,
            state=state,
            bom_df=bom_df,
            metrics=metrics_mapped,
        )
        st.success(f"🏷️ Site {selected_site_id} saved to Park successfully")
        st.caption(
            f"Park progress: "
            f"{st.session_state.park_data['sites_designed']} / "
            f"{len(st.session_state.park_data['sites'])} sites designed"
        )
    except Exception as e:
        st.error(f"⚠️ Failed to save site {selected_site_id} to Park")
        st.exception(e)

    # Direct write — guaranteed fallback for Page-5
    try:
        utm_epsg   = state.get_value("utm_epsg")
        layout_utm = state.get_value("layout_utm")
        if layout_utm is not None:
            try:
                state.set_value("layout_wgs", layout_utm.to_crs(epsg=4326))
            except Exception as e:
                logger.warning("layout_wgs sync failed: %s", e)

        park_data = st.session_state.get("park_data", {})

        park_data.setdefault("site_layouts", {})
        park_data["site_layouts"][selected_site_id] = {
            "layout_utm":       state.get_value("layout_utm"),
            "layout_wgs":       state.get_value("layout_wgs"),
            "metrics":          metrics_mapped,
            "params":           state.get_value("params"),
            "inverter_configs": state.get_value("inverter_configs"),
            "infra_generated":  state.get_value("infra_generated", False),
            "infra_results":    state.get_value("infra_results"),
            "cable_results":    state.get_value("cable_results"),
            "walkways":         state.get_value("walkways", []),
            "bom_df":           locals().get("bom_df"),
            "locked":           True,
            "saved_at":         datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
        park_data["sites_designed"] = len(park_data["site_layouts"])

        # Extract DP yard from AC infra
        _infra_r = state.get_value("infra_results") or {}
        _ac_gdf  = _infra_r.get("ac_items")
        _dp_found = False
        if _ac_gdf is not None and hasattr(_ac_gdf, "iterrows"):
            _crs = (str(_ac_gdf.crs) if hasattr(_ac_gdf, "crs") and _ac_gdf.crs is not None
                    else f"EPSG:{utm_epsg}")
            for _, _row in _ac_gdf.iterrows():
                if "DP" not in str(_row.get("name", "")).upper():
                    continue
                _geom = _row.geometry
                if _geom is None or getattr(_geom, "is_empty", True):
                    continue
                _cen_wgs = gpd.GeoSeries([_geom.centroid], crs=_crs).to_crs(epsg=4326).iloc[0]
                park_data["site_layouts"][selected_site_id]["dp_yard"] = {
                    "lat": float(_cen_wgs.y),
                    "lon": float(_cen_wgs.x),
                    "site_id": selected_site_id,
                    "source": "Page-4 AC infra",
                }
                _dp_found = True
                break

        logger.info(
            "park['site_layouts']['%s'] written. Total designed: %d",
            selected_site_id, park_data["sites_designed"],
        )
    except Exception as e:
        logger.error("Direct park site_layouts write failed: %s", e)


# ---------------------------------------------------------------------------
# render_manual_placement
# ---------------------------------------------------------------------------

def render_manual_placement(state, ui, layout_gen, params, inverter_configs):
    """Render manual zone drawing interface."""
    st.info("""
    **Manual Placement Instructions:**
    1. Select zone type from dropdown
    2. Use polygon/rectangle tool on map below
    3. Draw the zone on the map
    4. Click 'Save Last Drawn Zone'
    5. Repeat for all zones
    6. Set gate & borewell positions
    """)

    zone_options = ui.get_zone_options(params)

    col1, col2 = st.columns([3, 1])
    with col1:
        zone_type = st.selectbox("Select zone type:", zone_options)
    with col2:
        if st.button("🗑️ Clear All Zones", width="stretch"):
            state.clear_zones()
            st.success("✅ Zones cleared")
            st.rerun()

    drawn_zones = state.get_value("drawn_zones", {})
    if drawn_zones:
        st.write("**Defined Zones:**")
        for zone_name in list(drawn_zones.keys()):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"✅ {zone_name}")
            with c2:
                if st.button("🗑️", key=f"del_{zone_name}"):
                    del drawn_zones[zone_name]
                    state.set_value("drawn_zones", drawn_zones)
                    st.rerun()

    ui.render_point_infrastructure_inputs(state)

    if len(drawn_zones) > 0:
        if st.button(f"🚀 Generate Layout ({len(drawn_zones)} zones)", type="primary", width="stretch"):
            st.info("Manual mode generation — to be implemented")


# ---------------------------------------------------------------------------
# render_interactive_map
# ---------------------------------------------------------------------------

def render_interactive_map(state, ui, layout_gen, utm_epsg):
    """Render the interactive Folium map with state recovery."""

    # ── Layer visibility controls ─────────────────────────────────────────
    st.write("**🔲 Map Layer Controls:**")
    col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
    with col1: show_tables     = st.checkbox("Tables",       value=True)
    with col2: show_cables     = st.checkbox("Cables",       value=True)
    with col3: show_walkways   = st.checkbox("Walkways",     value=True)
    with col4: show_ac_infra   = st.checkbox("AC Infrastructure", value=True)
    with col5: show_trenches   = st.checkbox("Trenches",     value=False)
    with col6: show_gate       = st.checkbox("Gate",         value=True)
    with col7: show_bore       = st.checkbox("Borewell",     value=True)

    colA, colB, colC = st.columns(3)
    with colA: show_scbs          = st.checkbox("SCBs (String Combiners)", value=True)
    with colB: show_inverters     = st.checkbox("Inverters",               value=True)
    with colC: show_control_room  = st.checkbox("Control Room",            value=True)

    # ── Data recovery ─────────────────────────────────────────────────────
    boundary_ll  = state.get_value("boundary_ll")
    layout_wgs   = state.get_value("layout_wgs")
    infra_results = cable_results = walkways = None

    if hasattr(state, "get_infrastructure_with_recovery"):
        try:
            infra_results, cable_results, walkways = state.get_infrastructure_with_recovery()
        except Exception as e:
            logger.warning("Enhanced recovery failed: %s", e)

    if infra_results is None:
        infra_results = state.get_value("infra_results")
        if infra_results is None or not isinstance(infra_results, dict):
            infra_results = st.session_state.get("infra_results_backup") or {}
            if infra_results:
                state.set_value("infra_results", infra_results)

        cable_results = state.get_value("cable_results")
        if cable_results is None or not isinstance(cable_results, dict):
            cable_results = st.session_state.get("cable_results_backup") or {}
            if cable_results:
                state.set_value("cable_results", cable_results)

        walkways = state.get_value("walkways")
        if walkways is None:
            walkways = st.session_state.get("walkways_backup", [])
            if walkways:
                state.set_value("walkways", walkways)

    if not infra_results or not isinstance(infra_results, dict):
        st.warning("⚠️ No infrastructure data available. Generate layout first.")
        if hasattr(state, "recover_infrastructure"):
            c1, c2 = st.columns([1, 3])
            with c1:
                if st.button("🔄 Attempt Recovery"):
                    ri, rc, rw = state.recover_infrastructure()
                    if ri:
                        st.success("✅ Data recovered!")
                        st.rerun()
                    else:
                        st.error("❌ No recoverable data found")
            with c2:
                st.info("Click 'Attempt Recovery' to restore from backups")

    # ── Render map ────────────────────────────────────────────────────────
    try:
        map_data = ui.render_folium_map(
            boundary_ll=boundary_ll,
            layout_wgs=layout_wgs,
            infra_results=infra_results,
            cable_results=cable_results,
            walkways=walkways,
            utm_epsg=state.get_value("utm_epsg"),
            show_tables=show_tables,
            show_cables=show_cables,
            show_walkways=show_walkways,
            show_ac_infrastructure=show_ac_infra,
            show_trenches=show_trenches,
            show_gate=show_gate,
            show_borewell=show_bore,
            show_scbs=show_scbs,
            show_inverters=show_inverters,
            show_control_room=show_control_room,
        )
    except Exception as e:
        logger.exception("Map rendering failed")
        st.error(f"❌ Map rendering failed: {e}")
        with st.expander("🔍 Error Details"):
            import traceback
            st.code(traceback.format_exc())
        return None

    # ── Manual mode drawing handler ───────────────────────────────────────
    placement_mode = st.session_state.get("placement_mode", "🤖 Automatic (Recommended)")
    if map_data and "last_active_drawing" in map_data and "Manual" in placement_mode:
        st.info("💡 Feature drawn on map — click 'Save Last Drawn Zone' to save")

# ---------------------------------------------------------------------------
# render_export_section
# ---------------------------------------------------------------------------

def render_export_section(state, ui):
    """Render export options."""
    st.subheader("📤 Export Options")
    st.write("**📋 Select layers to export:**")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        exp_boundary = st.checkbox("Boundary",        True,  key="exp_layer_boundary")
        exp_tables   = st.checkbox("Tables",          True,  key="exp_layer_tables")
    with col2:
        exp_dc       = st.checkbox("DC Infrastructure", True, key="exp_layer_dc")
        exp_ac       = st.checkbox("AC Infrastructure", True, key="exp_layer_ac")
    with col3:
        exp_cables   = st.checkbox("Cables",          True,  key="exp_layer_cables")
        exp_trenches = st.checkbox("Trenches",        True,  key="exp_layer_trenches")
    with col4:
        exp_walkways = st.checkbox("Walkways",        True,  key="exp_layer_walkways")
        exp_gate     = st.checkbox("Gate",            True,  key="exp_layer_gate")
        exp_borewell = st.checkbox("Borewell",        True,  key="exp_layer_borewell")

    export_mgr = ExportManager()
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if st.button("📥 Export DXF", type="primary", width="stretch"):
            with st.spinner("Generating DXF..."):
                success, result = export_mgr.export_dxf(
                    state,
                    export_boundary=exp_boundary, export_tables=exp_tables,
                    export_dc_infra=exp_dc,       export_ac_infra=exp_ac,
                    export_cables=exp_cables,     export_trenches=exp_trenches,
                    export_walkways=exp_walkways, export_gate=exp_gate,
                    export_borewell=exp_borewell,
                )
                if success:
                    st.download_button("⬇️ Download DXF", data=result,
                                       file_name="site_layout.dxf", mime="application/dxf")
                else:
                    st.error(f"❌ DXF export failed: {result}")

    # Additional export buttons delegated to UIComponents
    ui.render_export_section(state)


# ---------------------------------------------------------------------------
# render_layout_designer
# ---------------------------------------------------------------------------

def render_layout_designer(state: StateManager, ui: UIComponents):
    """Render the layout designer tab (Tab 1)."""
    _sync_page4_state_bridge(state)
    st.header("🔍 Site Layout & Infrastructure Placement")
    park     = st.session_state.get("park_data", {})
    utm_epsg = state.get_value("utm_epsg")

    with st.expander("📖 Quick Start Guide", expanded=False):
        ui.render_quick_start_guide()

    uploaded = None
    selected_site_id = None

    if PARK_MODE:
        st.subheader("🏷️ Select Site from Park")
        site_ids         = list(park["site_kmz_files"].keys())
        selected_site_id = st.selectbox("Site ID", site_ids)

        _saved_layout = (park.get("site_layouts", {}) or {}).get(selected_site_id)
        _is_locked    = bool(_saved_layout and _saved_layout.get("locked", False))

        if _is_locked:
            _m = _saved_layout.get("metrics", {}) or {}
            st.success(
                f"✅ **{selected_site_id} layout is saved and locked.** "
                f"Tables: {_m.get('total_tables', _m.get('tables', '—'))} | "
                f"DC: {_m.get('dc_kwp', '—')} kWp | "
                f"Saved: {_saved_layout.get('saved_at', '—')}"
            )
            _col1, _col2 = st.columns([1, 4])
            with _col1:
                if st.button("🔓 Unlock & Redesign", type="secondary", key="unlock_layout"):
                    park["site_layouts"][selected_site_id]["locked"] = False
                    for _k in ["layout_utm", "layout_wgs", "metrics", "infra_generated"]:
                        state.set_value(_k, None)
                    st.session_state["layout_generated"] = False
                    st.session_state["layout_view_only"] = False
                    st.rerun()
            with _col2:
                st.caption("Layout is locked to prevent accidental changes. Click 🔓 to redesign.")

            for _k, _v in [
                ("layout_utm",       _saved_layout.get("layout_utm")),
                ("layout_wgs",       _saved_layout.get("layout_wgs")),
                ("params",           _saved_layout.get("params")),
                ("inverter_configs", _saved_layout.get("inverter_configs")),
                ("infra_generated",  _saved_layout.get("infra_generated")),
                ("infra_results",    _saved_layout.get("infra_results")),
                ("cable_results",    _saved_layout.get("cable_results")),
            ]:
                state.set_value(_k, _v)

            _restored_metrics = _saved_layout.get("metrics", {}) or {}
            _restored_infra = _saved_layout.get("infra_results")
            _restored_cable = _saved_layout.get("cable_results")

            state.set_value("metrics", _restored_metrics)
            _restored_metrics = _normalize_page4_metrics(state)

            # Keep the redundant session-state bridges in sync as well.
            # BOM, Shadow, Generation and dashboard components use these
            # bridges during fragment/tab reruns.
            if _restored_infra is not None:
                st.session_state["infra_results_backup"] = _restored_infra
            if _restored_cable is not None:
                st.session_state["cable_results_backup"] = _restored_cable
            if _saved_layout.get("walkways") is not None:
                st.session_state["walkways_backup"] = _saved_layout.get("walkways")
            st.session_state["layout_generated"] = True
            st.session_state["layout_metrics"]   = _restored_metrics
            st.session_state["selected_site_id"] = selected_site_id
            st.session_state["layout_view_only"] = True
        else:
            st.session_state["layout_view_only"] = False

    

        # Invalidate transient layout state only when the selected site really
        # changed AND there is no already-saved/locked layout being restored.
        # A locked Park-Mode site must win over the generic site-change reset;
        # otherwise the restore above is immediately erased, leaving the
        # dashboards at zero while the map can still show stale geometry.
        prev_site_id = st.session_state.get("prev_site_id")
        _site_changed = selected_site_id != prev_site_id
        if _site_changed and not _is_locked:
            for key in [
                "layout_utm", "layout_wgs", "metrics", "infra_generated",
                "infra_results", "cable_results", "walkways",
            ]:
                state.set_value(key, None)
            for key in [
                "infra_results_backup", "cable_results_backup",
                "walkways_backup", "layout_metrics", "layout_generated",
            ]:
                st.session_state.pop(key, None)
            st.info("ℹ️ Site changed. Click **Generate Layout** to update.")

        # Persist the active site after the locked/unlocked decision so a
        # restored saved layout is not treated as a new-site transition.
        st.session_state["prev_site_id"] = selected_site_id

        if selected_site_id:
            st.session_state["selected_site_id"] = selected_site_id
            uploaded = park["site_kmz_files"][selected_site_id]
            st.success(f"Loaded site KMZ for {selected_site_id}")

            cfg = park.get("site_config", {}).get(selected_site_id)
            if cfg:
                st.info(
                    f"⚡ **{selected_site_id} Capacity (from Page-3):** "
                    f"AC: **{cfg['ac_mw']:.2f} MW** | "
                    f"DC: **{cfg['dc_mw']:.2f} MW** | "
                    f"DC/AC: **{cfg['dc_ac_ratio']:.2f}**"
                )
                DEFAULT_INV_KW  = 295
                default_inv_qty = int(round(cfg["ac_mw"] * 1000 / DEFAULT_INV_KW))
                st.session_state["park_default_inverter_kw"]  = DEFAULT_INV_KW
                st.session_state["park_default_inverter_qty"] = default_inv_qty
                st.session_state["park_default_dc_kwp"]       = int(round(cfg["dc_mw"] * 1000))
                st.session_state["park_default_ac_kwp"]       = float(cfg["ac_mw"]) * 1000.0
                st.session_state["park_default_dc_ac"]        = float(cfg["dc_ac_ratio"])
    else:
        uploaded = st.file_uploader("Upload your site boundary file", type=["kmz", "kml"])

    if not uploaded:
        st.info("👆 Upload a KMZ/KML file to begin")
        return

    # ── Resolve boundary ──────────────────────────────────────────────────
    boundary_ll = None
    sites_gdf  = park.get("sites")      if isinstance(park, dict) else None
    sites_utm  = park.get("sites_utm")  if isinstance(park, dict) else None
    usable_utm = park.get("usable_area_utm") if isinstance(park, dict) else None

    if PARK_MODE:
        site_row = sites_gdf[sites_gdf["site_id"] == selected_site_id]
        if site_row.empty:
            st.error("❌ Selected site not found in park data")
            return
        boundary_ll = site_row.iloc[0].geometry
        st.success(f"Loaded site geometry for {selected_site_id} (Park Mode)")

        try:
            if usable_utm is not None and sites_utm is not None:
                site_row_utm = sites_utm[sites_utm["site_id"] == selected_site_id]
                if not site_row_utm.empty:
                    site_geom_utm  = site_row_utm.iloc[0].geometry
                    site_usable_utm = usable_utm.intersection(site_geom_utm)
                    if not site_usable_utm.is_empty:
                        site_usable_utm = site_usable_utm.buffer(-5)
                        _epsg = state.get_value("utm_epsg") or calculate_utm_epsg(
                            float(boundary_ll.centroid.x), float(boundary_ll.centroid.y)
                        )
                        boundary_ll = (
                            gpd.GeoSeries([site_usable_utm], crs=f"EPSG:{_epsg}")
                            .to_crs(epsg=4326)
                            .iloc[0]
                        )
                        st.success(f"✅ Using corridor-safe layout for {selected_site_id}")
                    else:
                        st.warning("⚠️ Usable area empty after clipping → fallback to site boundary")
        except Exception as e:
            st.warning(f"⚠️ Corridor clipping failed: {e}")
    else:
        with st.spinner("📖 Parsing boundary..."):
            success, result = parse_boundary_file(uploaded)
        if not success:
            st.error(f"❌ {result}")
            return
        polygons, kml_bytes = result
        st.success(f"✅ Found {len(polygons)} polygon(s)")
        poly_index = 0
        if len(polygons) > 1:
            poly_index = st.number_input("Select polygon index", 0, len(polygons) - 1, 0)
        boundary_ll = polygons[poly_index]

    centroid_ll = boundary_ll.centroid
    with st.spinner("🌍 Calculating UTM zone..."):
        utm_epsg = calculate_utm_epsg(float(centroid_ll.x), float(centroid_ll.y))
        st.success(f"UTM Zone: EPSG:{utm_epsg}")
        state.set_value("utm_epsg", utm_epsg)

    # ── View-only mode (layout locked) ────────────────────────────────────
    if st.session_state.get("layout_view_only", False):
        layout_utm = state.get_value("layout_utm")
        if layout_utm is not None:
            try:
                boundary_utm = gpd.GeoSeries([boundary_ll], crs="EPSG:4326").to_crs(epsg=utm_epsg).iloc[0]  # noqa F841
            except Exception:
                pass
            ui.render_metrics(state.get_value("metrics") or {})
            st.subheader("🗺️ Interactive Map (View Only)")
            render_interactive_map(state, ui, layout_gen, utm_epsg)
            if state.get_value("infra_generated", False):
                st.markdown("---")
                render_export_section(state, ui)
        else:
            st.info("No saved layout data to display.")
        return

    # ── Layout parameters ─────────────────────────────────────────────────
    st.subheader("🔧 Layout Parameters")
    params = ui.render_layout_parameters()
    params["inverter_alignment"] = params.get("inverter_alignment", "auto")

    st.subheader("⚡ Inverter Configuration")
    inverter_configs = ui.render_inverter_configuration()
    state.set_value("inverter_configs", inverter_configs)

    # ── Generate table layout ─────────────────────────────────────────────
    from modules.layout.table_generation import generate_table_layout

    with st.spinner("🔄 Generating table layout..."):
        success, result = generate_table_layout(
            boundary_ll=boundary_ll, params=params, utm_epsg=utm_epsg
        )
        if not success:
            st.error(f"❌ {result}")
            return
        layout_utm, layout_wgs, boundary_utm = result
        state.set_value("boundary_ll",  boundary_ll)
        state.set_value("boundary_utm", boundary_utm)
        state.set_value("layout_utm",   layout_utm)
        state.set_value("layout_wgs",   layout_wgs)
        state.set_value("params",       params)
        st.success(f"✅ Generated {len(layout_utm)} tables")

    # ── Metrics ───────────────────────────────────────────────────────────
    from modules.layout.metrics import calculate_metrics
    metrics = calculate_metrics(layout_utm=layout_utm, params=params, inverter_configs=inverter_configs)
    state.set_value("metrics", metrics)
    ui.render_metrics(metrics)

    # ── Placement mode ────────────────────────────────────────────────────
    st.subheader("🎨 Infrastructure Placement")
    placement_mode = st.radio(
        "Choose placement method:",
        ["🤖 Automatic (Recommended)", "✏️ Manual Zone Drawing"],
        help="Automatic mode optimally places all infrastructure",
    )
    st.session_state["placement_mode"] = placement_mode

    if placement_mode == "🤖 Automatic (Recommended)":
        render_automatic_placement(state, layout_gen, params, inverter_configs)
    else:
        render_manual_placement(state, ui, layout_gen, params, inverter_configs)

    st.subheader("🗺️ Interactive Map")
    render_interactive_map(state, ui, layout_gen, utm_epsg)

    if state.get_value("infra_generated", False):
        st.markdown("---")
        render_export_section(state, ui)


# ---------------------------------------------------------------------------
# render_bom_generator
# ---------------------------------------------------------------------------

def render_bom_generator(state: StateManager, ui: UIComponents):
    """Render BOM generator tab."""
    _sync_page4_state_bridge(state)
    st.header("📦 Bill of Materials Generator")

    # BOM requires the persisted infrastructure/layout data, not merely the
    # transient UI flag.  Recover from the redundant session-state backups when
    # a Streamlit tab/fragment rerun has left the primary StateManager flag out
    # of sync.
    _infra_ready = bool(state.get_value("infra_generated", False))
    if not _infra_ready:
        _infra_backup = st.session_state.get("infra_results_backup")
        _cable_backup = st.session_state.get("cable_results_backup")
        _walk_backup = st.session_state.get("walkways_backup")
        if isinstance(_infra_backup, dict) and _cable_backup is not None:
            state.set_value("infra_results", _infra_backup)
            state.set_value("cable_results", _cable_backup)
            state.set_value("walkways", _walk_backup)
            state.set_value("infra_generated", True)
            _infra_ready = True

    # A completed layout always has infrastructure data.  This fallback also
    # handles layouts restored from a saved/locked site.
    if not _infra_ready and st.session_state.get("layout_generated"):
        _saved_infra = state.get_value("infra_results")
        _saved_cable = state.get_value("cable_results")
        if isinstance(_saved_infra, dict) and _saved_cable is not None:
            state.set_value("infra_generated", True)
            _infra_ready = True

    if not _infra_ready:
        st.info("⚠️ Generate layout first to create BOM")
        return

    st.subheader("➕ Additional BOM Items (Optional)")
    with st.expander("Upload Additional Items CSV", expanded=False):
        st.write("""
        Upload a CSV file with additional BOM items not auto-generated.

        **Required columns:** Description, Unit, Qty

        **Optional columns:** Rate, GST %, Section, Notes
        """)
        uploaded_bom = st.file_uploader(
            "Upload additional BOM items (CSV)",
            type=["csv"],
            help="CSV with columns: Description, Unit, Qty, Rate, GST %, Section",
        )
        user_bom_df = None
        if uploaded_bom:
            try:
                from io import StringIO
                content    = uploaded_bom.getvalue().decode("utf-8")
                user_bom_df = pd.read_csv(StringIO(content))
                required   = ["Description", "Unit", "Qty"]
                missing    = [c for c in required if c not in user_bom_df.columns]
                if missing:
                    st.error(f"❌ Missing required columns: {', '.join(missing)}")
                    user_bom_df = None
                else:
                    for col, default in [("Rate", 0.0), ("GST %", 18.0),
                                         ("Section", "USER ITEMS"), ("Notes", "User uploaded")]:
                        if col not in user_bom_df.columns:
                            user_bom_df[col] = default
                    st.success(f"✅ Loaded {len(user_bom_df)} additional items")
                    st.dataframe(user_bom_df.head())
            except Exception as e:
                st.error(f"❌ Failed to load CSV: {e}")
                user_bom_df = None

    bom_gen = BOMGenerator()
    with st.spinner("📋 Generating BOM..."):
        bom_df = bom_gen.generate_comprehensive_bom(state)
        if user_bom_df is not None:
            for col in bom_df.columns:
                if col not in user_bom_df.columns:
                    user_bom_df[col] = 0.0 if col in ["Rate", "Total", "GST %", "GST Value", "Total (Incl. GST)"] else ""
            bom_df = pd.concat([bom_df, user_bom_df[bom_df.columns]], ignore_index=True)
            st.info(f"📌 Added {len(user_bom_df)} user items to BOM")

    ui.render_bom_display(bom_df, state)
    ui.render_bom_exports(bom_df, state)


# ---------------------------------------------------------------------------
# render_settings
# ---------------------------------------------------------------------------

def render_settings(state: StateManager, ui: UIComponents):
    """Render settings tab."""
    _sync_page4_state_bridge(state)
    st.header("⚙️ Application Settings")
    ui.render_settings_panel(state)
    st.subheader("ℹ️ About")
    ui.render_about_section()

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    """Main application entry point."""
    state = StateManager()
    state.initialize()
    ui    = UIComponents()

    # Establish the shared Page-4 bridge before any tab is rendered.
    _sync_page4_state_bridge(state)
    ui.render_header()

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "🗺️ Layout Designer",
        "📦 BOM Generator",
        "⚫ Shadow Analysis",
        "☀️ Generation",
        "🔋 BESS",
        "⚙️ Settings",
    ])
    from modules.visualization.layout_dashboard import render_layout_dashboard
    from modules.visualization.cable_dashboard import render_cluster_dashboard
    from modules.visualization.electrical_dashboard import render_electrical_dashboard

    # Streamlit st.tabs() executes every tab on every script rerun.  Page-4
    # contains several expensive maps/analyses, so isolate each tab in a
    # fragment.  Interactions inside one tab then rerun only that tab instead
    # of rebuilding all six tabs (especially the 171-table shading analysis).
    @st.fragment
    def _render_layout_tab():
        _sync_page4_state_bridge(state)
        render_layout_designer(state, ui)
        if st.session_state.get("layout_generated"):
            render_layout_dashboard()
            render_cluster_dashboard()
            render_electrical_dashboard()

    @st.fragment
    def _render_bom_tab():
        _sync_page4_state_bridge(state)
        render_bom_generator(state, ui)

    from modules.shading.shading_dashboard import render_shading_dashboard
    from modules.shading.shading_heatmap_dashboard import (
        render_shading_heatmap_section,
        render_terrain_shading_section,
    )

    @st.fragment
    def _render_shadow_tab():
        _sync_page4_state_bridge(state)
        if st.session_state.get("layout_generated"):
            try:
                render_shading_dashboard(state)
                render_shading_heatmap_section(state)
                render_terrain_shading_section(state)
            except Exception as exc:
                logger.exception("Shadow Analysis tab failed")
                st.error(f"❌ Shadow Analysis could not be rendered: {exc}")
                st.info("The shared Page-4 state bridge has been restored. Retry the Shadow Analysis tab.")
        else:
            st.info("ℹ️ Generate or load a site layout first to run Shadow Analysis.")

    from modules.energy.energy_calculations import render_generation_calculations

    @st.fragment
    def _render_generation_tab():
        _sync_page4_state_bridge(state)
        render_generation_calculations(state)

    from modules.bess.bess_tab import render_bess_tab

    def _render_bess_tab():
        _sync_page4_state_bridge(state)
        # Keep BESS as a normal tab body.  The BESS module is lightweight and
        # this avoids fragment isolation issues on Streamlit versions where
        # nested tab/fragment rendering can make the tab appear blank.
        render_bess_tab()

    @st.fragment
    def _render_settings_tab():
        _sync_page4_state_bridge(state)
        render_settings(state, ui)

    with tab1:
        _sync_page4_state_bridge(state)
        _render_layout_tab()
    with tab2:
        _sync_page4_state_bridge(state)
        _render_bom_tab()
    with tab3:
        _sync_page4_state_bridge(state)
        _render_shadow_tab()
    with tab4:
        _sync_page4_state_bridge(state)
        _render_generation_tab()
    with tab5:
        _sync_page4_state_bridge(state)
        _render_bess_tab()
    with tab6:
        _sync_page4_state_bridge(state)
        _render_settings_tab()

if __name__ == "__main__":
    main()
