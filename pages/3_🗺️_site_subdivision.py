import streamlit as st
import pandas as pd
from modules.park_infrastructure.park_generator import export_site_kmz

# -------------------------------------------------
# Page config
# -------------------------------------------------
st.set_page_config(
    page_title="Capacity Configuration",
    page_icon="📐",
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

st.title("📐 Site Capacity Configuration")

# -------------------------------------------------
# Session guards
# -------------------------------------------------
park = st.session_state.get("park_data")

if park is None or not park.get("infrastructure_done"):
    st.warning("⚠️ Complete Park Infrastructure (Page-2) first.")
    st.stop()

sites_gdf = park.get("sites")
if sites_gdf is None or sites_gdf.empty:
    st.warning("⚠️ No sites available.")
    st.stop()

FP_TOL = 0.005

# -------------------------------------------------
# Capacity Rules
# -------------------------------------------------
st.subheader("⚙️ Capacity Rules")
c1, c2, c3 = st.columns(3)
with c1:
    acres_per_mw = st.number_input("Acres per MW AC", value=2.5, step=0.1, min_value=0.5)
with c2:
    min_dc_ac = st.number_input("Min DC/AC", value=1.1, step=0.05, min_value=0.5)
with c3:
    max_dc_ac = st.number_input("Max DC/AC", value=1.6, step=0.05, min_value=1.0)

# -------------------------------------------------
# Initialise committed state (once per infrastructure run)
# -------------------------------------------------
def _fresh_row(site_row):
    area  = round(float(site_row["area_acres"]), 2)
    ac    = round(float(site_row["ac_mw"]), 2)
    dc    = float(site_row["dc_mw"])
    dc_ac = round(dc / ac, 2) if ac > 0 else 1.30
    return {
        "site_id":    site_row["site_id"],
        "area_acres": area,
        "max_ac_mw":  round(area / acres_per_mw, 2),
        "user_ac_mw": ac,
        "user_dc_ac": dc_ac,
        "dc_mw_calc": round(ac * dc_ac, 2),
        "status":     "✅ OK",
    }

if "site_config_df" not in park:
    park["site_config_df"]       = pd.DataFrame([_fresh_row(r) for _, r in sites_gdf.iterrows()])
    park["unallocated_acres"]    = 0.0
    park["unallocated_pool_log"] = []
    park["editor_version"]       = 0   # bumped on Apply Changes to force widget reset

# Always work from committed state
committed: pd.DataFrame = park["site_config_df"].copy()
unallocated: float      = float(park.get("unallocated_acres", 0.0))
pool_log: list          = list(park.get("unallocated_pool_log", []))

# Page-2 can manually remove land from inside a site. In that case the
# authoritative geometry/area is the current Page-2 sites GeoDataFrame.
# Reconcile the committed Page-3 table with it before rendering the map so
# navigation never resurrects the pre-edit area/capacity values.
if park.get("geometry_manual_edited"):
    _live_sites = park.get("sites")
    if _live_sites is not None and not _live_sites.empty:
        _live_lookup = _live_sites.set_index("site_id")
        for _i, _r in committed.iterrows():
            _sid = _r["site_id"]
            if _sid not in _live_lookup.index:
                continue
            _live_row = _live_lookup.loc[_sid]
            committed.at[_i, "area_acres"] = round(float(_live_row.get("area_acres", _r["area_acres"])), 2)
            committed.at[_i, "user_ac_mw"] = round(float(_live_row.get("ac_mw", _r["user_ac_mw"])), 2)
            committed.at[_i, "dc_mw_calc"] = round(float(_live_row.get("dc_mw", _r["dc_mw_calc"])), 2)
            _live_ac = float(committed.at[_i, "user_ac_mw"])
            _live_dc = float(committed.at[_i, "dc_mw_calc"])
            committed.at[_i, "user_dc_ac"] = round((_live_dc / _live_ac) if _live_ac > 0 else 1.30, 2)
            committed.at[_i, "status"] = "✏️ MANUALLY EDITED"
        park["site_config_df"] = committed.copy()

# Refresh max_ac_mw if acres_per_mw widget changed
committed["max_ac_mw"] = (committed["area_acres"] / acres_per_mw).round(2)

total_park_acres = round(committed["area_acres"].sum() + unallocated, 2)

# -------------------------------------------------
# UNALLOCATED POOL BANNER
# -------------------------------------------------
if unallocated > FP_TOL:
    st.warning(
        f"🟡 **Unallocated Land Pool: {unallocated:.2f} acres "
        f"(≈ {unallocated / acres_per_mw:.2f} MW AC potential)**  \n"
        "Assign this land to an existing site or create a new site below."
    )
    if pool_log:
        with st.expander("Pool history"):
            for entry in pool_log:
                st.caption(entry)

# -------------------------------------------------
# SITE TABLE  — data_editor is a DRAFT only
# Changes are NOT applied until user clicks "Apply Changes"
# -------------------------------------------------
st.subheader("🧩 Site Configuration Table")
st.caption(
    "Edit **Desired AC (MW)** and **DC/AC Ratio**, then click **▶ Apply Changes**. "
    "Reducing a site's AC frees land which cascades to the next site sequentially."
)

editor_cols = ["site_id", "area_acres", "max_ac_mw", "user_ac_mw", "user_dc_ac", "dc_mw_calc", "status"]

draft = st.data_editor(
    committed[editor_cols],
    width="stretch",
    column_config={
        "site_id":    st.column_config.TextColumn("Site ID", disabled=True),
        "area_acres": st.column_config.NumberColumn("Area (acres)", disabled=True, format="%.2f"),
        "max_ac_mw":  st.column_config.NumberColumn("Max AC (MW)", disabled=True, format="%.2f",
                          help="Area ÷ acres-per-MW"),
        "user_ac_mw": st.column_config.NumberColumn("✏️ Desired AC (MW)", format="%.2f",
                          help="Reducing this frees land to the next site below."),
        "user_dc_ac": st.column_config.NumberColumn("✏️ DC/AC Ratio", format="%.2f"),
        "dc_mw_calc": st.column_config.NumberColumn("DC (MW)", disabled=True, format="%.2f"),
        "status":     st.column_config.TextColumn("Status", disabled=True),
    },
    disabled=["site_id", "area_acres", "max_ac_mw", "dc_mw_calc", "status"],
    hide_index=True,
    key=f"site_editor_{park.get('editor_version', 0)}",
)

# ▶ APPLY CHANGES — only now does reallocation run
if st.button("▶ Apply Changes", type="primary"):

    DUST_THRESHOLD = 0.10  # acres

    def run_reallocation(df: pd.DataFrame, pool: float, apm: float):
        """Resolve Desired AC into a conserved site-area allocation.

        The committed geometry areas are the starting point. Each requested
        AC is converted to required acres. A reduction transfers its released
        land to the next *unchanged* site in sequence; an increase draws from
        later sites that have surplus area above their own requested minimum,
        then from the pool. If adjacent sites are both explicitly edited,
        their final areas follow their own requests and only the net residual
        is transferred. This prevents the old double-cascade/double-count bug.
        """
        df = df.copy().reset_index(drop=True)
        n = len(df)
        msgs = []
        current = [max(0.0, float(df.at[i, "area_acres"])) for i in range(n)]
        desired = [max(0.0, float(df.at[i, "user_ac_mw"])) for i in range(n)]
        target = [d * apm for d in desired]
        pool = max(0.0, float(pool))
        original_pool = pool

        # Desired AC cannot exceed the total land that remains after reserving
        # the requested minimum land for all other sites.
        total_available = sum(current) + pool
        for i in range(n):
            others_required = sum(target[j] for j in range(n) if j != i)
            max_target = max(0.0, total_available - others_required)
            if target[i] > max_target + FP_TOL:
                old = desired[i]
                desired[i] = round(max_target / apm, 2)
                target[i] = desired[i] * apm
                msgs.append(
                    f"⚠️ {df.at[i,'site_id']}: {old:.2f} MW → "
                    f"{desired[i]:.2f} MW (insufficient total land)"
                )

        # Work from committed areas. Explicit reductions/increases are applied
        # once; transfers are accounted for separately so land is never counted
        # twice when neighbouring sites are both edited.
        final_area = list(current)
        changed = [abs(target[i] - current[i]) > FP_TOL for i in range(n)]

        # First satisfy explicit target areas for every changed site.
        # Positive deltas require funding; negative deltas create transferable
        # land. Funding uses later sites' original surplus or the pool.
        deficits = [max(0.0, target[i] - current[i]) for i in range(n)]
        surpluses = [max(0.0, current[i] - target[i]) for i in range(n)]

        for i in range(n):
            if not changed[i] or deficits[i] <= FP_TOL:
                continue
            remaining = deficits[i]
            for j in range(i + 1, n):
                take = min(remaining, surpluses[j])
                if take > FP_TOL:
                    surpluses[j] -= take
                    final_area[j] -= take
                    remaining -= take
                    msgs.append(
                        f"⬅️ {df.at[i,'site_id']} takes {take:.2f} ac from "
                        f"{df.at[j,'site_id']}"
                    )
                if remaining <= FP_TOL:
                    break
            if remaining > FP_TOL and pool > FP_TOL:
                take = min(remaining, pool)
                pool -= take
                remaining -= take
                msgs.append(
                    f"⬅️ {df.at[i,'site_id']} takes {take:.2f} ac from pool"
                )
            if remaining > FP_TOL:
                # No legal source remains. Clamp to the land actually funded.
                funded = deficits[i] - remaining
                target[i] = current[i] + funded
                desired[i] = round(target[i] / apm, 2)
                deficits[i] = funded
                msgs.append(
                    f"⚠️ {df.at[i,'site_id']}: increase partially funded; "
                    f"retained at {desired[i]:.2f} MW"
                )

        # Apply target areas for changed sites. Unchanged sites retain their
        # original area. Surplus from a reduction is transferred only to the
        # immediate next site when that next site was not explicitly edited.
        for i in range(n):
            if changed[i]:
                final_area[i] = target[i]

        for i in range(n - 1):
            freed = max(0.0, current[i] - target[i]) if changed[i] else 0.0
            if freed <= FP_TOL:
                continue
            if not changed[i + 1]:
                final_area[i + 1] += freed
                msgs.append(
                    f"➡️ {df.at[i,'site_id']} freed {freed:.2f} ac → "
                    f"{df.at[i+1,'site_id']}"
                )
            else:
                # The next site already has an explicit target. Its reduction
                # is not double-counted; the freed land remains available to a
                # later site or the pool.
                placed = False
                for j in range(i + 2, n):
                    if not changed[j]:
                        final_area[j] += freed
                        msgs.append(
                            f"➡️ {df.at[i,'site_id']} freed {freed:.2f} ac → "
                            f"{df.at[j,'site_id']}"
                        )
                        placed = True
                        break
                if not placed:
                    pool += freed
                    msgs.append(
                        f"➡️ {df.at[i,'site_id']} freed {freed:.2f} ac → pool"
                    )

        # Surplus from the final site goes to the pool.
        if n:
            freed_last = max(0.0, current[-1] - target[-1]) if changed[-1] else 0.0
            if freed_last > FP_TOL:
                pool += freed_last
                msgs.append(
                    f"➡️ {df.at[n-1,'site_id']} freed {freed_last:.2f} ac → pool"
                )

        # Any original surplus that was used to fund an earlier increase is
        # already reflected in final_area. Remaining surplus on later sites is
        # retained by those sites unless they were explicitly reduced.
        for i in range(n):
            final_area[i] = round(max(0.0, final_area[i]), 2)
            df.at[i, "area_acres"] = final_area[i]
            df.at[i, "max_ac_mw"] = round(final_area[i] / apm, 2)
            df.at[i, "user_ac_mw"] = round(desired[i], 2)

        # Conservation correction after rounding.
        # pool currently contains the real pool after increases/reductions.
        end_total = round(sum(final_area) + pool, 2)
        target_total = round(sum(current) + original_pool, 2)
        correction = round(target_total - end_total, 2)
        if abs(correction) > FP_TOL and n:
            pool = round(max(0.0, pool + correction), 2)

        return df, round(pool, 2), msgs

    reallocated, unallocated, msgs = run_reallocation(draft, unallocated, acres_per_mw)

    # Validate
    for i, r in reallocated.iterrows():
        dc_ac   = float(r["user_dc_ac"])
        desired = float(r["user_ac_mw"])
        parts   = []
        if desired <= 0:
            parts.append("❌ AC must be > 0")
        if dc_ac < min_dc_ac - FP_TOL:
            parts.append(f"❌ DC/AC < {min_dc_ac}")
        elif dc_ac > max_dc_ac + FP_TOL:
            parts.append(f"❌ DC/AC > {max_dc_ac}")
        reallocated.at[i, "dc_mw_calc"] = round(desired * dc_ac, 2)
        reallocated.at[i, "status"]     = " | ".join(parts) if parts else "✅ OK"

    # Commit to session state
    park["site_config_df"]       = reallocated
    park["unallocated_acres"]    = unallocated
    pool_log += msgs
    park["unallocated_pool_log"] = pool_log

    # Keep the live site metadata synchronized immediately. The geometry is
    # deliberately NOT regenerated here: Page-2 manual geometry must remain
    # intact until the user explicitly freezes the configuration.
    live_sites = park.get("sites")
    if live_sites is not None and not live_sites.empty:
        cfg_lookup = reallocated.set_index("site_id")
        for _idx, _site in live_sites.iterrows():
            _sid = _site.get("site_id")
            if _sid in cfg_lookup.index:
                _cfg = cfg_lookup.loc[_sid]
                live_sites.at[_idx, "area_acres"] = float(_cfg["area_acres"])
                live_sites.at[_idx, "ac_mw"] = float(_cfg["user_ac_mw"])
                live_sites.at[_idx, "dc_mw"] = float(_cfg["dc_mw_calc"])

    # Freeze uses exactly the capacities/areas that were just applied.
    park["site_targets"] = [
        {
            "site_id": r["site_id"],
            "ac_mw": float(r["user_ac_mw"]),
            "area_acres": float(r["area_acres"]),
        }
        for _, r in reallocated.iterrows()
    ]
    park["geometry_stale"] = True

    # Bump editor version so data_editor and the capacity map re-render.
    park["editor_version"] = park.get("editor_version", 0) + 1

    if msgs:
        st.info("🔄 Reallocation applied:\n\n" + "\n\n".join(f"- {m}" for m in msgs))

    st.rerun()

# -------------------------------------------------
# ADD SITE / DELETE SITE
# These operate on committed state directly (no reallocation needed)
# -------------------------------------------------
st.markdown("---")
col_add, col_del, _ = st.columns([1, 2, 3])

with col_add:
    if st.button("➕ Add Site", help="Creates a new site using all unallocated pool land"):
        if unallocated < 0.5:
            st.error("❌ Need ≥ 0.5 unallocated acres to add a site.")
        else:
            existing_ids = list(committed["site_id"])
            idx = len(committed) + 1
            new_id = f"SITE_{idx:02d}"
            while new_id in existing_ids:
                idx += 1
                new_id = f"SITE_{idx:02d}"
            new_area  = round(unallocated, 2)
            new_ac    = round(new_area / acres_per_mw, 2)
            new_dc_ac = 1.30
            new_row   = pd.DataFrame([{
                "site_id":    new_id,
                "area_acres": new_area,
                "max_ac_mw":  new_ac,
                "user_ac_mw": new_ac,
                "user_dc_ac": new_dc_ac,
                "dc_mw_calc": round(new_ac * new_dc_ac, 2),
                "status":     "✅ OK",
            }])
            updated = pd.concat([committed, new_row], ignore_index=True)
            pool_log.append(f"➕ {new_id} created with {new_area:.2f} acres from pool")
            park["site_config_df"]       = updated
            park["unallocated_acres"]    = 0.0
            park["unallocated_pool_log"] = pool_log
            park["editor_version"]       = park.get("editor_version", 0) + 1
            st.success(f"✅ {new_id} added ({new_area:.2f} acres, {new_ac:.2f} MW AC).")
            st.rerun()

with col_del:
    if not committed.empty:
        del_choice = st.selectbox(
            "Site to delete", options=committed["site_id"].tolist(),
            label_visibility="collapsed", key="del_select"
        )
        if st.button("🗑 Delete Selected Site", type="secondary"):
            freed   = float(committed[committed["site_id"] == del_choice]["area_acres"].iloc[0])
            updated = committed[committed["site_id"] != del_choice].reset_index(drop=True)
            unallocated = round(unallocated + freed, 2)
            pool_log.append(f"🗑 {del_choice} deleted → {freed:.2f} acres to pool")
            park["site_config_df"]       = updated
            park["unallocated_acres"]    = unallocated
            park["unallocated_pool_log"] = pool_log
            park["editor_version"]       = park.get("editor_version", 0) + 1
            st.info(f"🗑 {del_choice} deleted. {freed:.2f} acres added to unallocated pool.")
            st.rerun()

# -------------------------------------------------
# ASSIGN FROM POOL to an existing site
# -------------------------------------------------
if unallocated > FP_TOL and not committed.empty:
    st.markdown("---")
    st.subheader("🔀 Assign Pool Land to a Site")
    col_s, col_a, col_b = st.columns([2, 1, 1])
    with col_s:
        assign_target = st.selectbox(
            "Target site", options=committed["site_id"].tolist(), key="assign_target"
        )
    with col_a:
        _pool_val = round(float(unallocated), 2)
        _min_val  = min(0.1, _pool_val)   # never let min_value exceed the actual pool
        assign_acres = st.number_input(
            "Acres to assign",
            min_value=_min_val,
            max_value=_pool_val,
            value=_pool_val,
            step=min(0.1, _pool_val),
            key="assign_acres"
        )
    with col_b:
        st.write("")
        st.write("")
        if st.button("✅ Assign to Site"):
            updated = committed.copy()
            idx     = updated[updated["site_id"] == assign_target].index[0]
            updated.at[idx, "area_acres"] = round(float(updated.at[idx, "area_acres"]) + assign_acres, 2)
            updated.at[idx, "max_ac_mw"]  = round(updated.at[idx, "area_acres"] / acres_per_mw, 2)
            unallocated = round(unallocated - assign_acres, 2)
            pool_log.append(f"🔀 {assign_acres:.2f} acres from pool → {assign_target}")
            park["site_config_df"]       = updated
            park["unallocated_acres"]    = unallocated
            park["unallocated_pool_log"] = pool_log
            park["editor_version"]       = park.get("editor_version", 0) + 1
            st.success(f"✅ {assign_acres:.2f} acres assigned to {assign_target}.")
            st.rerun()

# -------------------------------------------------
# LIVE SUMMARY TABLE (read-only, always reflects committed state)
# -------------------------------------------------
st.markdown("---")
st.subheader("📋 Current Configuration")

summary = park["site_config_df"][[
    "site_id", "area_acres", "max_ac_mw",
    "user_ac_mw", "user_dc_ac", "dc_mw_calc", "status"
]].copy()
summary.columns = ["Site ID", "Area (acres)", "Max AC (MW)", "Desired AC (MW)", "DC/AC", "DC MW", "Status"]
st.dataframe(summary, width="stretch", hide_index=True)

allocated_acres = park["site_config_df"]["area_acres"].sum()
st.caption(
    f"🗺️ Total park: **{total_park_acres:.2f} acres** | "
    f"Allocated: **{allocated_acres:.2f} ac** | "
    f"Pool: **{unallocated:.2f} ac**"
)

total_ac = park["site_config_df"]["user_ac_mw"].sum()
total_dc = park["site_config_df"]["dc_mw_calc"].sum()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total AC", f"{total_ac:.2f} MW")
c2.metric("Total DC", f"{total_dc:.2f} MW")
c3.metric("Avg DC/AC", f"{(total_dc / total_ac):.2f}" if total_ac else "—")
c4.metric("Sites", len(park["site_config_df"]))


# -------------------------------------------------
# SITE MAP — with labels, area, capacity
# Version-stamped container forces full re-render on every commit.
# -------------------------------------------------
st.markdown("---")
st.subheader("🗺️ Site Capacity Map")
_map_version = park.get("editor_version", 0)
st.caption(f"Map version: {_map_version} — updates on every Apply Changes")

# Use a unique container key via empty + rewrite trick
_map_container = st.container()

try:
    import folium
    from streamlit_folium import st_folium

    sites_gdf_map = park.get("sites")
    cfg_map       = park["site_config_df"].copy()   # fresh copy every render
    boundary_map  = park.get("boundary")

    if sites_gdf_map is not None and not sites_gdf_map.empty and boundary_map is not None:

        cfg_lookup = cfg_map.set_index("site_id").to_dict("index")

        m = folium.Map(
            location=[boundary_map.centroid.y, boundary_map.centroid.x],
            zoom_start=15,
            control_scale=True,
        )
        # Inject version stamp as invisible HTML element.
        # This changes the map's HTML hash on every commit, forcing
        # streamlit-folium to re-render instead of serving cached iframe.
        m.get_root().html.add_child(
            folium.Element(
                f'<!-- surya_map_v{_map_version} -->'
            )
        )

        # Park boundary
        folium.Polygon(
            locations=[(lat, lon) for lon, lat in boundary_map.exterior.coords],
            color="black", weight=2, fill=False, tooltip="Park Boundary"
        ).add_to(m)

        # Sites with colour labels
        COLOURS = [
            "#2ecc71", "#3498db", "#e67e22", "#9b59b6",
            "#1abc9c", "#e74c3c", "#f1c40f", "#34495e",
        ]

        for idx, row in sites_gdf_map.iterrows():
            sid    = row["site_id"]
            cfg_r  = cfg_lookup.get(sid, {})
            area   = cfg_r.get("area_acres", row.get("area_acres", 0))
            ac_mw  = cfg_r.get("user_ac_mw", row.get("ac_mw", 0))
            dc_mw  = cfg_r.get("dc_mw_calc", row.get("dc_mw", 0))
            dc_ac  = cfg_r.get("user_dc_ac", 1.30)
            status = cfg_r.get("status", "OK")
            colour = COLOURS[idx % len(COLOURS)]

            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            # Flatten MultiPolygon → draw each part separately
            parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
            # Use largest part for label anchor; draw all parts
            geom = max(parts, key=lambda g: g.area)

            for part in parts:
                folium.Polygon(
                    locations=[(lat, lon) for lon, lat in part.exterior.coords],
                    color=colour, weight=2,
                    fill=True, fill_color=colour, fill_opacity=0.35,
                    tooltip=f"{sid}",
                    popup=folium.Popup(
                        f"<div style='font-family:monospace;font-size:13px;min-width:160px'>"
                        f"<b style='color:{colour}'>{sid}</b><br>"
                        f"Area: <b>{area:.2f} ac</b><br>"
                        f"AC: <b>{ac_mw:.2f} MW</b><br>"
                        f"DC: <b>{dc_mw:.2f} MW</b><br>"
                        f"DC/AC: <b>{dc_ac:.2f}</b><br>"
                        f"{status}</div>",
                        max_width=200
                    )
                ).add_to(m)

            cen = geom.centroid
            label_html = (
                f'<div style="font-size:11px;font-weight:bold;color:white;'
                f'background:{colour};border-radius:4px;padding:2px 6px;'
                f'white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.4);'
                f'text-align:center;line-height:1.4;">'
                f'{sid}<br>'
                f'<span style="font-weight:normal;font-size:10px">'
                f'{area:.1f} ac | {ac_mw:.2f} MW</span></div>'
            )
            folium.Marker(
                location=[cen.y, cen.x],
                icon=folium.DivIcon(html=label_html, icon_size=(110, 36), icon_anchor=(55, 18))
            ).add_to(m)

        # Roads
        roads = park.get("roads", {})
        if roads is None:
            roads = {}

        if "main_access" in roads and roads["main_access"] is not None:
            for geom in roads["main_access"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#FF8C00", weight=4, tooltip="Main Access Road"
                ).add_to(m)

        if "spine" in roads and roads["spine"] is not None:
            for geom in roads["spine"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#444444", weight=3, tooltip="Spine Road"
                ).add_to(m)

        if "site_spurs" in roads and roads["site_spurs"] is not None:
            for geom in roads["site_spurs"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#888888", weight=2, tooltip="Site Spur"
                ).add_to(m)

        # Drainage
        drainage = park.get("drainage", {})
        if drainage is None:
            drainage = {}

        if "channels" in drainage and drainage["channels"] is not None:
            for geom in drainage["channels"]:
                try:
                    folium.PolyLine(
                        locations=[(lat, lon) for lon, lat in geom.coords],
                        color="#0066CC", weight=2,
                        dash_array="5,5", tooltip="Drainage Channel"
                    ).add_to(m)
                except Exception:
                    pass

        # Electrical
        electrical = park.get("electrical", {})
        if electrical is None:
            electrical = {}

        if "oh_line_33kv" in electrical and electrical["oh_line_33kv"] is not None:
            for geom in electrical["oh_line_33kv"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#CC5500", weight=3, tooltip="33kV OH Line"
                ).add_to(m)

        if "droppers" in electrical and electrical["droppers"] is not None:
            for geom in electrical["droppers"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="red", weight=2,
                    dash_array="3,3", tooltip="Dropper to Site Pole Yard"
                ).add_to(m)

        if "evacuation_line" in electrical and electrical["evacuation_line"] is not None:
            for geom in electrical["evacuation_line"]:
                folium.PolyLine(
                    locations=[(lat, lon) for lon, lat in geom.coords],
                    color="#8B0000", weight=4, tooltip="33kV Evacuation Line"
                ).add_to(m)

        if "poles" in electrical and electrical["poles"] is not None:
            for geom in electrical["poles"]:
                folium.CircleMarker(
                    location=(geom.y, geom.x),
                    radius=2, color="#CC5500",
                    fill=True, fill_opacity=0.8, tooltip="33kV Pole"
                ).add_to(m)

        if "site_pole_yards" in electrical and electrical["site_pole_yards"] is not None:
            for geom in electrical["site_pole_yards"]:
                folium.RegularPolygonMarker(
                    location=(geom.y, geom.x),
                    number_of_sides=4, radius=6,
                    color="orange", fill=True,
                    fill_color="yellow", fill_opacity=0.9,
                    tooltip="Site Pole Yard"
                ).add_to(m)

        # Facilities
        facilities = park.get("facilities", {})
        if facilities is None:
            facilities = {}

        if "main_gate" in facilities and facilities["main_gate"] is not None:
            geom = facilities["main_gate"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>Main Gate</b><br>Park Entry Point",
                icon=folium.Icon(color="orange", icon="road", prefix="fa", icon_color="white"),
                tooltip="Main Gate"
            ).add_to(m)
            folium.CircleMarker(
                location=(geom.y, geom.x),
                radius=8, color="orange", fill=True,
                fill_color="orange", fill_opacity=0.3, weight=2
            ).add_to(m)

        if "switchyard" in facilities and facilities["switchyard"] is not None:
            geom = facilities["switchyard"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>Switchyard</b><br>33kV Pooling Station",
                icon=folium.Icon(color="purple", icon="bolt", prefix="fa", icon_color="white"),
                tooltip="Switchyard"
            ).add_to(m)
            folium.CircleMarker(
                location=(geom.y, geom.x),
                radius=10, color="purple", fill=True,
                fill_color="purple", fill_opacity=0.3, weight=3
            ).add_to(m)

        if "om_building" in facilities and facilities["om_building"] is not None:
            geom = facilities["om_building"]
            folium.Marker(
                location=(geom.y, geom.x),
                popup="<b>O&M Building</b>",
                icon=folium.Icon(color="blue", icon="building", prefix="fa", icon_color="white"),
                tooltip="O&M Building"
            ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, height=550, width=None, returned_objects=[])

    else:
        st.info("Map will appear here after infrastructure is generated.")

except Exception as _map_err:
    st.error(f"Map could not render: {_map_err}")
    import traceback
    st.code(traceback.format_exc())

# -------------------------------------------------
# FREEZE
# -------------------------------------------------
st.markdown("---")
st.subheader("🔒 Freeze Configuration")

final_df = park["site_config_df"]
has_errors = (final_df["status"].str.startswith("❌")).any()

if has_errors:
    st.error("❌ Resolve all validation errors before freezing.")
else:
    if unallocated > FP_TOL:
        st.warning(
            f"⚠️ {unallocated:.2f} acres still in pool. "
            "You may freeze — unallocated land will be logged in summary."
        )

    if st.button("🔒 Freeze & Regenerate Geometry", type="primary"):
        park["site_kmz_files"] = {}
        park["site_config"]    = {}

        # ── Write per-site capacity config ─────────────────────────────
        for _, r in final_df.iterrows():
            park["site_config"][r["site_id"]] = {
                "area_acres":  r["area_acres"],
                "ac_mw":       r["user_ac_mw"],
                "dc_mw":       r["dc_mw_calc"],
                "dc_ac_ratio": r["user_dc_ac"],
            }

        # ── Write site_targets for Page-2 target-aware geometry ────────
        park["site_targets"] = [
            {
                "site_id":    r["site_id"],
                "ac_mw":      r["user_ac_mw"],
                "area_acres": r["area_acres"],
            }
            for _, r in final_df.iterrows()
        ]

        # ── Partition hints (kept for backward compat + manual Page-2) ─
        park["partition_hints"] = {
            "acres_per_mw":      acres_per_mw,
            "min_site_ac_mw":    float(final_df["user_ac_mw"].min()),
            "max_site_ac_mw":    float(final_df["user_ac_mw"].max()),
            "target_site_count": len(final_df),
        }

        # ── AUTO-TRIGGER: regenerate geometry to match targets ─────────
        _regen_success = False
        _regen_error   = None
        try:
            from modules.park_infrastructure.park_generator import (
                ParkInfrastructureGenerator, export_site_kmz as _export_kmz
            )
            from shapely.geometry import Point as _Point

            _boundary    = park.get("boundary")
            _buildable   = park.get("buildable_zones")
            _utm_epsg    = park.get("utm_epsg")
            _entry_lat   = park.get("entry_lat", _boundary.centroid.y)
            _entry_lon   = park.get("entry_lon", _boundary.centroid.x)
            _hints       = park.get("partition_hints", {})
            _spine_bias  = park.get("last_spine_bias")
            _spine_ori   = park.get("last_spine_orientation", "horizontal")

            if park.get("geometry_manual_edited"):
                # Preserve the Page-2 manually edited geometry. Re-running the
                # original target partitioner here would recreate the removed
                # land because it starts from the full buildable envelope.
                #
                # Freeze must nevertheless produce fresh KMZs from the EDITED
                # site polygons. Previously this branch skipped KMZ creation,
                # so Freeze appeared to do nothing (or left stale downloads).
                _edited_sites = park.get("sites")
                if _edited_sites is None or _edited_sites.empty:
                    raise ValueError("Edited site geometry is unavailable. Apply the Page-2 internal area edit first.")

                _edited_kmz = {}
                for _, _edited_site in _edited_sites.iterrows():
                    _sid = str(_edited_site.get("site_id", ""))
                    if _sid and _sid in park["site_config"]:
                        _edited_kmz[_sid] = _export_kmz(_edited_site, _edited_sites)

                park["site_kmz_files"] = _edited_kmz
                park["sites"] = _edited_sites
                park["infrastructure_done"] = True
                park["infrastructure_complete"] = True
                park["geometry_stale"] = False
                _regen_success = True
            elif _boundary and _buildable is not None and _utm_epsg:
                with st.spinner("🔄 Regenerating geometry from targets…"):
                    _gen   = ParkInfrastructureGenerator()
                    _infra = _gen.generate_complete_infrastructure(
                        park_boundary          = _boundary,
                        buildable_zones        = _buildable,
                        main_road_entry_point  = _Point(_entry_lon, _entry_lat),
                        utm_epsg               = _utm_epsg,
                        acres_per_mw           = acres_per_mw,
                        min_site_mw            = float(_hints.get("min_site_ac_mw", 1.0)),
                        max_site_mw            = float(_hints.get("max_site_ac_mw", 10.0)),
                        spine_bias             = _spine_bias,
                        spine_orientation      = _spine_ori,
                        site_targets           = park["site_targets"],
                    )

                # Update park geometry with new results
                park["sites"]      = _infra["sites"]
                park["roads"]      = _infra["roads"]
                park["drainage"]   = _infra["drainage"]
                park["electrical"] = _infra["electrical"]
                park["facilities"] = _infra["facilities"]
                park["park_summary"] = _infra["infra_summary"]
                park["infrastructure_done"] = True

                # Regenerate KMZs with new geometry
                _new_sites = _infra["sites"]
                for _, site in _new_sites.iterrows():
                    if site["site_id"] in park["site_config"]:
                        _kmz = _export_kmz(site, _new_sites)
                        park["site_kmz_files"][site["site_id"]] = _kmz

                # Invalidate Page-4 layouts (geometry changed)
                park.pop("site_layouts", None)
                park.pop("sites_designed", None)
                park["geometry_stale"] = False
                park["editor_version"] = park.get("editor_version", 0) + 1
                _regen_success = True
            else:
                _regen_error = "Missing boundary/buildable/UTM data. Run Page-1 & Page-2 first."

        except Exception as _e:
            _regen_error = str(_e)

        if _regen_success:
            # Push confirmed capacity values into new sites GeoDataFrame
            _new_sites = park["sites"]
            for i, r in _new_sites.iterrows():
                sid = r["site_id"]
                if sid in park["site_config"]:
                    _new_sites.at[i, "ac_mw"] = park["site_config"][sid]["ac_mw"]
                    _new_sites.at[i, "dc_mw"] = park["site_config"][sid]["dc_mw"]
            park["sites_generated"] = True
            st.success("✅ Configuration frozen & geometry regenerated. Page-4 enabled.")
        else:
            # Geometry regen failed — still freeze capacity, warn user
            for i, r in sites_gdf.iterrows():
                sid = r["site_id"]
                if sid in park["site_config"]:
                    park["sites"].at[i, "ac_mw"] = park["site_config"][sid]["ac_mw"]
                    park["sites"].at[i, "dc_mw"] = park["site_config"][sid]["dc_mw"]
            for _, site in sites_gdf.iterrows():
                if site["site_id"] in park["site_config"]:
                    park["site_kmz_files"][site["site_id"]] = export_site_kmz(site, sites_gdf)
            park["sites_generated"] = True
            park["geometry_stale"]  = True
            if _regen_error:
                st.warning(f"⚠️ Capacity frozen but geometry regeneration failed: {_regen_error}")
            else:
                st.warning("⚠️ Capacity frozen but geometry could not be regenerated automatically.")
            st.info("👉 Go to Page-2 and click Generate to apply the new site targets.")

        st.rerun()

if park.get("sites_generated") and park.get("site_kmz_files"):
    st.subheader("⬇️ Site KMZ Downloads")
    for site_id, kmz in park["site_kmz_files"].items():
        st.download_button(
            label=f"⬇️ {site_id}.kmz",
            data=kmz.getvalue(),
            file_name=f"{site_id}.kmz",
            mime="application/vnd.google-earth.kmz",
            key=f"kmz_dl_{site_id}"
        )

# -------------------------------------------------
# CONTROLLED RETURN TO PAGE-2
# -------------------------------------------------
st.markdown("---")
st.subheader("🔄 Repartition Geometry (Advanced)")

st.warning(
    "This will discard all park infrastructure and return to Page-2.\n\n"
    "Your capacity learnings will be preserved as defaults."
)

confirm = st.checkbox("I understand and want to reset")

if confirm and st.button("↩️ Reset & Return to Page-2"):
    park["partition_hints"] = {
        "acres_per_mw":      acres_per_mw,
        "min_site_ac_mw":    float(final_df["user_ac_mw"].min()),
        "max_site_ac_mw":    float(final_df["user_ac_mw"].max()),
        "target_site_count": len(final_df),
        "reason":            "Manual repartition requested",
    }
    for k in [
        "sites", "roads", "drainage", "electrical", "facilities", "park_summary",
        "site_kmz_files", "site_config_df", "site_config",
        "sites_generated", "unallocated_acres", "unallocated_pool_log",
    ]:
        park.pop(k, None)
    park["infrastructure_done"] = False
    st.success("✅ Reset complete. Navigate to Page-2.")
    st.rerun()
