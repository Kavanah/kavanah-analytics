// Docs site client-side enhancements.
//
// 1. Fix home links that point to "#" — redirect to docs root instead.
// 2. Role-based nav filtering: fetch the current user's allowed sections
//    from the Flask app and hide sidebar + landing-page entries for the
//    sections they should not see. This is nav filtering only; direct
//    URLs still reach the underlying pages (docs are static nginx files).

(function () {
    // Section slug -> URL path prefix used in Sphinx toctree links.
    // Keys must match the server-side permissions response.
    var SECTION_PREFIXES = {
        'guide-dashboards': 'guide-dashboards/',
        'guide-data-explorer': 'guide-data-explorer/',
        'guide-management-console': 'guide-management-console/',
        'self-hosted': 'self-hosted/'
    };

    // Flask is served behind nginx at the /assets/ prefix (see
    // PrefixMiddleware in app.py). Docs pages are static nginx output
    // at /docs/, so we must call the prefixed URL explicitly.
    var PERMISSIONS_URL = '/assets/api/documentation-permissions';
    var CACHE_KEY = 'kavDocsSectionPerms';

    document.addEventListener('DOMContentLoaded', function () {
        fixHomeLinks();
        suppressSelfReferentialSidebarLinks();
        // Apply cached permissions synchronously to avoid a flash of the
        // full sidebar on every page load. The cache is refreshed in
        // the background so role changes propagate on the next page view.
        var cached = readCachedSections();
        if (cached) {
            hideDisallowedSidebarSections(cached);
            hideDisallowedLandingSections(cached);
        }
        refreshPermissions();
    });

    function readCachedSections() {
        try {
            var raw = window.localStorage && window.localStorage.getItem(CACHE_KEY);
            if (!raw) return null;
            var parsed = JSON.parse(raw);
            return parsed && parsed.sections ? parsed.sections : null;
        } catch (e) {
            return null;
        }
    }

    function writeCachedSections(sections) {
        try {
            window.localStorage.setItem(CACHE_KEY, JSON.stringify({ sections: sections }));
        } catch (e) {
            // localStorage may be unavailable (private mode, quota) — ignore.
        }
    }

    function fixHomeLinks() {
        document.querySelectorAll('a[href="#"]').forEach(function (a) {
            if (a.classList.contains('icon-home') || a.textContent.trim() === 'Kavanah Analytics') {
                a.href = '/docs/';
            }
        });
    }

    // Sphinx marks the current page's sidebar entry with href="#". Clicking it
    // leaves the user on the same page but tacks "#" onto the URL, which
    // looks broken. Swallow the click so the URL stays clean.
    function suppressSelfReferentialSidebarLinks() {
        var menu = document.querySelector('.wy-menu-vertical');
        if (!menu) return;
        menu.querySelectorAll('a[href="#"]').forEach(function (a) {
            a.addEventListener('click', function (e) {
                e.preventDefault();
            });
        });
    }

    function refreshPermissions() {
        fetch(PERMISSIONS_URL, { credentials: 'same-origin' })
            .then(function (res) {
                if (!res.ok) throw new Error('docs-perms http ' + res.status);
                return res.json();
            })
            .then(function (data) {
                var sections = (data && data.sections) || {};
                writeCachedSections(sections);
                hideDisallowedSidebarSections(sections);
                hideDisallowedLandingSections(sections);
            })
            .catch(function () {
                // Fail open: leave the full nav visible. Nav filtering is a
                // UX affordance, not access control.
            });
    }

    // Look through anchors inside a list and return the first section slug
    // whose URL prefix matches. Returns null if no known section matches.
    function identifySection(container) {
        var anchors = container.querySelectorAll('a[href]');
        for (var i = 0; i < anchors.length; i++) {
            var href = anchors[i].getAttribute('href') || '';
            // Normalise "../foo/bar" and "./foo/bar" to "foo/bar" so prefix
            // matching works from any page depth.
            var normalised = href.replace(/^(\.\.?\/)+/, '');
            for (var slug in SECTION_PREFIXES) {
                if (normalised.indexOf(SECTION_PREFIXES[slug]) === 0) {
                    return slug;
                }
            }
        }
        return null;
    }

    // Apply a section's visibility. Explicitly reveals when allowed so a
    // previously-hidden section reappears if permissions flip (e.g. a fresh
    // fetch returns true after the cache said false).
    function applySectionVisibility(el, allowed) {
        el.style.display = allowed ? '' : 'none';
    }

    // Sidebar: each `.. toctree::` in index.rst renders as a
    // <p class="caption"> heading followed by a sibling <ul>.
    function hideDisallowedSidebarSections(sections) {
        var menu = document.querySelector('.wy-menu-vertical');
        if (!menu) return;
        var children = menu.children;
        for (var i = 0; i < children.length; i++) {
            var el = children[i];
            if (el.tagName !== 'P' || !el.classList.contains('caption')) continue;
            var ul = el.nextElementSibling;
            if (!ul || ul.tagName !== 'UL') continue;
            var slug = identifySection(ul);
            if (!slug) continue;
            var allowed = sections[slug] !== false;
            applySectionVisibility(el, allowed);
            applySectionVisibility(ul, allowed);
        }
    }

    // Landing page: each toctree renders as <div class="toctree-wrapper compound">.
    // The caption (if any) is typically inside the wrapper as <p class="caption">.
    function hideDisallowedLandingSections(sections) {
        var wrappers = document.querySelectorAll('div.toctree-wrapper');
        wrappers.forEach(function (wrapper) {
            var slug = identifySection(wrapper);
            if (!slug) return;
            var allowed = sections[slug] !== false;
            applySectionVisibility(wrapper, allowed);
            // Some themes render the caption as a sibling heading rather
            // than inside the wrapper — toggle an adjacent caption too.
            var prev = wrapper.previousElementSibling;
            if (prev && prev.tagName === 'P' && prev.classList.contains('caption')) {
                applySectionVisibility(prev, allowed);
            }
        });
    }
})();
