"""PBIP-301 / PBIP-302: Engine B Report Layout Shadow Model Tests."""

import os
from mcp_server.engine_b import ReportLayoutEngine


def test_engine_b_pages_and_visuals():
    engine = ReportLayoutEngine(os.path.abspath("sample_project.pbip"))
    pages = engine.list_pages()

    assert len(pages) == 2
    assert pages[0]["display_name"] == "Executive Summary"
    assert pages[0]["visual_count"] == 3
    assert pages[1]["display_name"] == "Details"
    assert pages[1]["visual_count"] == 1

    visuals = engine.get_page_visuals("Executive Summary")
    visual_ids = [v.id for v in visuals]
    assert "vis_card_revenue" in visual_ids
    assert "vis_bar_revenue_region" in visual_ids
    assert "vis_slicer_region" in visual_ids


def test_engine_b_path_based_queries():
    engine = ReportLayoutEngine(os.path.abspath("sample_project.pbip"))

    # Query visual type
    v_type = engine.get_property("Executive Summary/vis_card_revenue/visual_type")
    assert v_type == "card"

    # Query position
    pos = engine.get_property("Executive Summary/vis_card_revenue/position")
    assert pos["width"] == 300.0
    assert pos["height"] == 180.0

    # Query nested position property
    width = engine.get_property("Executive Summary/vis_card_revenue/position/width")
    assert width == 300.0

    # Query bound fields
    bound = engine.get_property("Executive Summary/vis_card_revenue/bound_fields")
    assert "Sales.Total Revenue" in bound

    # Query slicer
    slicer_type = engine.get_property("Executive Summary/vis_slicer_region/visual_type")
    assert slicer_type == "slicer"
    slicer_bound = engine.get_property("Executive Summary/vis_slicer_region/bound_fields")
    assert "Customer.Region" in slicer_bound


def test_engine_b_jmespath_queries():
    engine = ReportLayoutEngine(os.path.abspath("sample_project.pbip"))

    # Find all slicers across all pages
    slicers = engine.query("pages[*].visuals[?is_slicer==`true`].id")
    # Flatten list of lists
    flat_slicers = [item for sublist in slicers for item in sublist]
    assert "vis_slicer_region" in flat_slicers

    # Find visuals referencing Sales.Total Revenue
    bound_rev = engine.query("pages[*].visuals[?contains(bound_fields, 'Sales.Total Revenue')].id")
    flat_bound = [item for sublist in bound_rev for item in sublist]
    assert "vis_card_revenue" in flat_bound
    assert "vis_bar_revenue_region" in flat_bound
    assert "vis_table_details" in flat_bound
