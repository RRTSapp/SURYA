#!/usr/bin/env python3
"""
Solar Park Master Planner - Main Application
Complete integration of terrain analysis, park infrastructure, and site design
"""

import streamlit as st
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========================================
# PAGE CONFIGURATION (MUST BE FIRST)
# ========================================
st.set_page_config(
    layout="wide", 
    page_title="SURYA – Solar Park Master Planner", 
    page_icon="☀️",
    initial_sidebar_state="expanded",
    menu_items={
        'About': """
        # SURYA – Solar Park Master Planner
        
        Complete solution for solar park planning from terrain analysis 
        to detailed site design and BOM generation.
        
        **Version:** 1.0.0 | Engineering baseline: Stage 4.4.4
        """
    }
)

# Authentication is enforced here and independently in every multipage script.
from auth.supabase_auth import require_authentication
require_authentication()

# ========================================
# SESSION STATE INITIALIZATION
# ========================================
def initialize_session_state():
    """
    Initialize all session state variables
    This runs once when the app starts
    """
    
    if 'park_data' not in st.session_state:
        st.session_state.park_data = {
            
            # ==========================================
            # LEVEL 1: TERRAIN ANALYSIS (Page 1)
            # ==========================================
            'boundary': None,                    # Shapely Polygon - overall park boundary
            'boundary_gdf': None,                # GeoDataFrame version
            'terrain_grid': None,                # DataFrame with lon, lat, elevation, gradient
            'buildable_zones': None,             # GeoDataFrame (gradient < threshold)
            'restricted_zones': None,            # GeoDataFrame (gradient > threshold)
            'elevation_contours': None,          # GeoDataFrame with elevation contour lines
            'main_road_entry': None,             # Point where park connects to public road
            'utm_epsg': None,                    # UTM zone EPSG code (e.g., 32643)
            'grid_size': 50,                     # Grid spacing in meters
            'gradient_threshold': 10,            # Buildable threshold (%)
            'terrain_complete': False,           # Flag
            
            # ==========================================
            # LEVEL 2: PARK INFRASTRUCTURE (Page 2)
            # ==========================================
            'sites': None,                       # GeoDataFrame with all site polygons
            'roads': None,                       # Dict with road geometries
            'drainage': None,                    # Dict with drainage geometries
            'electrical': None,                  # Dict with electrical routing
            'facilities': None,                  # Dict with common facilities
            'park_summary': None,                # Dict with park-level statistics
            'infrastructure_complete': False,    # Flag
            
            # ==========================================
            # LEVEL 3: SITE SUBDIVISION (Page 3)
            # ==========================================
            'site_kmz_files': {},                # Dict mapping site_id to KMZ BytesIO
            'selected_site': None,               # Currently selected site for design
            'sites_generated': False,            # Flag
            
            # ==========================================
            # LEVEL 4: SITE LAYOUTS (Page 4)
            # ==========================================
            'site_layouts': {},                  # Dict mapping site_id to layout data
            # Structure for each site:
            # 'SITE_01': {
            #     'layout_utm': GeoDataFrame,      # Table locations in UTM
            #     'layout_wgs': GeoDataFrame,      # Table locations in WGS84
            #     'infra_results': {               # Infrastructure
            #         'scb_points': GeoDataFrame,
            #         'inverter_points': GeoDataFrame,
            #         'dc_items': GeoDataFrame,
            #         'ac_items': GeoDataFrame,
            #     },
            #     'cable_results': {               # Cables and trenches
            #         'cables': GeoDataFrame,
            #         'trenches': GeoDataFrame,
            #     },
            #     'walkways': GeoDataFrame,
            #     'bom': DataFrame,                # Site-level BOM
            #     'metrics': Dict,                 # Site metrics
            # }
            'sites_designed': 0,                 # Count of completed sites
            
            # ==========================================
            # LEVEL 5: PARK CONSOLIDATION (Page 5)
            # ==========================================
            'master_bom': None,
            'master_exports': {},
            'park_complete': False,

            # ==========================================
            # RATE SHEET — single source of truth
            # All pages read from here; editable in Page-5.
            # ==========================================
            'rate_sheet': {
                'module_per_wp':           20.0,
                'inverter_per_kw':       4000.0,
                'dc_cable_per_m':          80.0,
                'ac_cable_per_m':         150.0,
                'mounting_per_wp':         12.0,
                'civil_per_mw':            15.0,
                'site_electrical_per_mw':  10.0,
                'road_per_km':             20.0,
                'drainage_per_km':          5.0,
                'culvert_each':         50000.0,
                'compound_wall_rm':       800.0,
                'chainlink_rm':           250.0,
                'gate_lump':                5.0,
                'oh_line_per_km':          15.0,
                'pole_each':            25000.0,
                'site_pole_yard':      150000.0,
                'switchyard_lump':         50.0,
                'evac_line_per_km':        15.0,
                'pooling_ss_lump':        200.0,
                'row_land_per_km':         10.0,
                'row_clear_per_km':          3.0,
                # 4C margins
                'dev_margin_per_mwdc':      10.0,
                'epc_margin_per_mwdc':      15.0,
                'contingency_per_mwdc':      5.0,
            },
            
            # ==========================================
            # METADATA
            # ==========================================
            'project_name': 'Untitled Park',
            'created_date': None,
            'last_modified': None,
            'total_capacity_mw': 50.0,           # Target park capacity
            'target_site_size_mw': 5.0,          # Target per site
        }
        
        logger.info("✅ Session state initialized successfully")
    
    # Also initialize flag for showing welcome only once
    if 'welcome_shown' not in st.session_state:
        st.session_state.welcome_shown = False

# ========================================
# HELPER FUNCTIONS
# ========================================
def get_progress_percentage():
    """Calculate overall progress through the workflow"""
    park = st.session_state.park_data
    
    steps = [
        park['terrain_complete'],
        park['infrastructure_complete'],
        park['sites_generated'],
        park['sites_designed'] > 0,
        park['park_complete'],
    ]
    
    completed = sum(steps)
    total = len(steps)
    
    return int((completed / total) * 100)

def reset_park_data():
    """Reset all park data - useful for starting a new project"""
    if st.session_state.get('park_data'):
        del st.session_state['park_data']
    initialize_session_state()
    logger.info("🔄 Park data reset")

# ========================================
# CALL INITIALIZATION
# ========================================
initialize_session_state()

# ========================================
# SIDEBAR - PROGRESS TRACKER
# ========================================
st.sidebar.title("☀️ SURYA – Solar Park Planner")

# Project info
park = st.session_state.park_data

with st.sidebar.expander("📁 Project Info", expanded=True):
    st.text_input(
        "Project Name",
        value=park.get('project_name', 'Untitled Park'),
        key='project_name_input',
        on_change=lambda: st.session_state.park_data.update(
            {'project_name': st.session_state.project_name_input}
        )
    )
    
    if park.get('created_date'):
        st.caption(f"Created: {park['created_date']}")

from modules.project_io import render_project_io
render_project_io()

st.sidebar.markdown("---")

# Progress tracker
st.sidebar.subheader("📍 Workflow Progress")

progress_items = [
    ("📊 Terrain Analysis", park['terrain_complete'], "Page 1"),
    ("🏗️ Park Infrastructure", park['infrastructure_complete'], "Page 2"),
    ("🗺️ Site Subdivision", park['sites_generated'], "Page 3"),
    ("⚡ Site Designs", park['sites_designed'] > 0, "Page 4"),
    ("📋 Master BOM", park['park_complete'], "Page 5"),
]

for label, complete, page in progress_items:
    status = "✅" if complete else "⬜"
    st.sidebar.text(f"{status} {label}")

# Overall progress bar
progress_pct = get_progress_percentage()
st.sidebar.progress(progress_pct / 100)
st.sidebar.caption(f"Overall Progress: {progress_pct}%")

from modules.ad_slots import render_ad_slot
render_ad_slot("sidebar", compact=True)

st.sidebar.markdown("---")

# Quick stats (if available)
if park.get('park_summary'):
    st.sidebar.subheader("📊 Park Summary")
    summary = park['park_summary']
    
    col1, col2 = st.sidebar.columns(2)
    col1.metric("Sites", summary.get('total_sites', 0))
    col2.metric("Capacity", f"{summary.get('total_ac_capacity_mw', 0):.0f} MW")
    
    st.sidebar.metric("Area", f"{summary.get('total_area_acres', 0):.1f} acres")
    st.sidebar.metric("Roads", f"{summary.get('roads_total_km', 0):.2f} km")

# Site design progress (if applicable)
if park['sites'] is not None and len(park['sites']) > 0:
    total_sites = len(park['sites'])
    designed = park['sites_designed']
    st.sidebar.markdown("---")
    st.sidebar.subheader("🎯 Site Design Status")
    st.sidebar.progress(designed / total_sites if total_sites > 0 else 0)
    st.sidebar.caption(f"{designed} of {total_sites} sites completed")

st.sidebar.markdown("---")

# Navigation helper
st.sidebar.info("""
**📖 How to use:**

1. Upload park boundary (Page 1)
2. Generate infrastructure (Page 2)  
3. Download site KMZ files (Page 3)
4. Design each site (Page 4)
5. Export master BOM (Page 5)
""")

# Reset button
if st.sidebar.button("🔄 Start New Project", help="Clear all data and start over"):
    if st.sidebar.checkbox("⚠️ Confirm reset"):
        reset_park_data()
        st.rerun()

# Footer
st.sidebar.markdown("---")
st.sidebar.caption("SURYA – Solar Park Master Planner v1.0")
st.sidebar.caption("Integrated Terrain → Infrastructure → Layout → BOM")

# ========================================
# MAIN CONTENT AREA
# ========================================
st.title("☀️ SURYA – Solar Park Master Planner")

# Welcome message (show only once)
if not st.session_state.welcome_shown:
    st.balloons()
    st.session_state.welcome_shown = True

st.markdown("""
Welcome to the **Solar Park Master Planner** - your complete solution for large-scale 
solar park design from terrain analysis to detailed BOM generation.

### 🎯 What This Tool Does

This integrated platform guides you through the entire solar park planning process:

1. **📊 Terrain Analysis** - Upload your park boundary and analyze elevation & gradients
2. **🏗️ Park Infrastructure** - Automatically design roads, drainage, and electrical routing  
3. **🗺️ Site Subdivision** - Divide the park into optimal site blocks with individual KMZ files
4. **⚡ Site Design** - Detailed layout for each site (tables, inverters, cables, BOM)
5. **📋 Master Consolidation** - Aggregate all sites into a complete park-level BOM

### 🚀 Getting Started

**👈 Select a page from the sidebar to begin!**

Start with **Page 1: Terrain Analysis** to upload your park boundary KMZ file.
""")

# Show quick start guide
with st.expander("📖 Quick Start Guide", expanded=False):
    st.markdown("""
    ### Step-by-Step Workflow
    
    #### Page 1: Terrain Analysis
    - Upload overall park boundary KMZ file
    - Set main road entry point
    - Configure grid size (25-200m)
    - Run elevation analysis
    - Review buildable vs restricted zones
    
    #### Page 2: Park Infrastructure  
    - Configure park capacity (MW)
    - Set target site size (MW per site)
    - Generate road network
    - Design drainage system
    - Route electrical infrastructure
    - Place common facilities
    
    #### Page 3: Site Subdivision
    - Review generated sites
    - Download individual site KMZ files
    - Verify site boundaries
    
    #### Page 4: Site Design
    - Select a site from dropdown
    - Upload site KMZ (from Page 3)
    - Configure table parameters
    - Generate layout
    - Export site DXF/KMZ/BOM
    - Repeat for all sites
    
    #### Page 5: Master Consolidation
    - Review all site layouts
    - Generate consolidated BOM
    - Add park infrastructure costs
    - Export master files (KMZ, DXF, Excel)
    
    ### 💡 Tips
    - Save your work frequently by exporting files
    - Start with larger grid size (100-200m) for quick testing
    - Use 50m grid for final detailed analysis
    - Design critical sites first to test parameters
    """)

# Current status cards
st.markdown("---")
st.subheader("📋 Current Project Status")

col1, col2, col3, col4 = st.columns(4)

with col1:
    if park['boundary']:
        st.success("✅ Boundary Loaded")
        # FIX: project to UTM before computing area
        # WGS84 .area returns square degrees, not square metres.
        try:
            import geopandas as gpd
            _epsg = park.get('utm_epsg') or 32643
            _area_m2 = (
                gpd.GeoSeries([park['boundary']], crs="EPSG:4326")
                .to_crs(epsg=_epsg)
                .iloc[0]
                .area
            )
            st.caption(f"{_area_m2 / 4047:.1f} acres")
        except Exception:
            st.caption("Area unavailable")
    else:
        st.info("⬜ No Boundary")
        st.caption("Upload on Page 1")

with col2:
    if park['terrain_complete']:
        st.success("✅ Terrain Analyzed")
        if park['terrain_grid'] is not None:
            st.caption(f"{len(park['terrain_grid'])} points")
    else:
        st.info("⬜ No Terrain Data")
        st.caption("Complete Page 1")

with col3:
    if park['infrastructure_complete']:
        st.success("✅ Infrastructure Generated")
        if park['sites'] is not None:
            st.caption(f"{len(park['sites'])} sites")
    else:
        st.info("⬜ No Infrastructure")
        st.caption("Complete Page 2")

with col4:
    designed = park['sites_designed']
    total = len(park['sites']) if park['sites'] is not None else 0
    if designed > 0:
        st.success(f"✅ {designed}/{total} Sites")
        st.caption("Designed")
    else:
        st.info("⬜ No Site Designs")
        st.caption("Complete Page 4")

# Show next action
st.markdown("---")
if not park['terrain_complete']:
    st.info("👉 **Next Step:** Go to **Page 1: Terrain Analysis** to upload your park boundary")
elif not park['infrastructure_complete']:
    st.info("👉 **Next Step:** Go to **Page 2: Park Infrastructure** to generate roads and facilities")
elif not park['sites_generated']:
    st.info("👉 **Next Step:** Go to **Page 3: Site Subdivision** to generate site KMZ files")
elif park['sites_designed'] < total:
    st.info(f"👉 **Next Step:** Go to **Page 4: Site Layout** to design remaining sites ({total - designed} left)")
elif not park['park_complete']:
    st.info("👉 **Next Step:** Go to **Page 5: Master Consolidation** to generate final BOM")
else:
    st.success("🎉 **Project Complete!** All sites designed and BOM generated.")

# Visual workflow diagram
st.markdown("---")
with st.expander("🗺️ View Workflow Diagram", expanded=False):
    st.markdown("""
    ```
    📤 Upload KMZ
         ↓
    📊 Terrain Analysis (Page 1)
         ↓
    🏗️ Park Infrastructure (Page 2)
         ├─ Roads
         ├─ Drainage  
         ├─ Electrical
         └─ Facilities
         ↓
    🗺️ Site Subdivision (Page 3)
         └─ Generate KMZ for each site
         ↓
    ⚡ Site Design (Page 4) - Repeat for each site
         ├─ Tables Layout
         ├─ Inverters & SCBs
         ├─ Cables & Trenches
         └─ Site BOM
         ↓
    📋 Master BOM (Page 5)
         ├─ Aggregate all sites
         ├─ Add park infrastructure
         └─ Export master files
    ```
    """)

# Debug info (only for development)
if st.checkbox("🔧 Show Debug Info", value=False):
    st.json({
        'session_keys': list(st.session_state.keys()),
        'park_data_keys': list(park.keys()),
        'terrain_complete': park['terrain_complete'],
        'infrastructure_complete': park['infrastructure_complete'],
        'sites_count': len(park['sites']) if park['sites'] is not None else 0,
        'sites_designed': park['sites_designed'],
    })

# ========================================
# FOOTER
# ========================================
render_ad_slot("home_footer_banner")
st.markdown("---")
st.caption("""
**SURYA – Solar Park Master Planner** | Integrated solution for terrain analysis, 
infrastructure planning, and detailed site design | Version 1.0.0
""")