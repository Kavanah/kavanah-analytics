# Configuration file for the Sphinx documentation builder.
# Kavanah Analytics — v1.0 Documentation
# Kavanah Media, 2026

import os
import sys

# -- Project information -------------------------------------------------------

project = 'Kavanah Analytics'
copyright = '2026, Kavanah Media'
author = 'Kavanah Media'
release = '1.0.0'
version = '1.0'

# -- General configuration -----------------------------------------------------

extensions = [
    'sphinx_rtd_theme',
    'sphinxcontrib.httpdomain',
    'sphinx.ext.autosectionlabel',
]

# Prefix autosectionlabel with the document name to prevent collisions
# across files that share section titles (e.g., "Overview" in multiple files).
autosectionlabel_prefix_document = True

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store', '_scribe']

# -- Options for HTML output ---------------------------------------------------

html_theme = 'sphinx_rtd_theme'

html_theme_options = {
    'logo_only': False,
    'prev_next_buttons_location': 'bottom',
    'style_external_links': False,
    'collapse_navigation': False,
    'sticky_navigation': True,
    'navigation_depth': 3,
    'includehidden': True,
    'titles_only': False,
}

html_static_path = ['_static']
html_js_files = ['custom.js']
html_css_files = ['custom.css']

html_show_sphinx = False
html_show_sourcelink = False

# Project logo (add a logo file to _static/ and uncomment):
# html_logo = '_static/kavanahanalytics-logo.png'

# -- Options for httpdomain ----------------------------------------------------

# Base URL prefix used in HTTP endpoint examples throughout the API reference.
# In production the app is served at https://<domain>/assets/ with nginx prefix.
# For documentation purposes we show paths without the /assets prefix.
http_index_ignore_prefixes = ['/api/', '/assets/']
