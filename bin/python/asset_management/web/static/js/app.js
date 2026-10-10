/**
 * Web Asset Manager - JavaScript functionality
 * Handles conditional fields, form validation, and AJAX operations.
 */

// -- HTML escaping utility to prevent stored XSS ---
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// --- Global CSRF protection for all fetch() calls ---
(function() {
    var _originalFetch = window.fetch;
    window.fetch = function(input, init) {
        init = init || {};
        var method = (init.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
            var meta = document.querySelector('meta[name="csrf-token"]');
            if (meta) {
                init.headers = init.headers || {};
                // Support both plain objects and Headers instances
                if (init.headers instanceof Headers) {
                    if (!init.headers.has('X-CSRF-Token')) {
                        init.headers.set('X-CSRF-Token', meta.getAttribute('content'));
                    }
                } else {
                    if (!init.headers['X-CSRF-Token']) {
                        init.headers['X-CSRF-Token'] = meta.getAttribute('content');
                    }
                }
            }
        }
        return _originalFetch.call(this, input, init);
    };
})();

document.addEventListener('DOMContentLoaded', function() {
    // Initialize conditional fields if we're on a form page
    if (document.getElementById('asset-form')) {
        initConditionalFields();
    }

    // Clear tree explorer state when logout link is clicked
    var logoutLink = document.getElementById('logout-link');
    if (logoutLink) {
        logoutLink.addEventListener('click', function() {
            // Clear tree explorer state from localStorage
            localStorage.removeItem('treeExplorerState');
        });
    }

    // Clear tree explorer state when session timeout is detected
    // Check if we're on the login page with a 'next' parameter pointing to dashboard
    if (window.location.pathname.includes('/login')) {
        var urlParams = new URLSearchParams(window.location.search);
        var nextUrl = urlParams.get('next');
        if (nextUrl && nextUrl.includes('show=dashboard')) {
            // Clear tree explorer state from localStorage
            localStorage.removeItem('treeExplorerState');
        }
    }
});

/**
 * Initialize conditional field visibility based on form state.
 */
function initConditionalFields() {
    // Initial evaluation
    updateConditionalFields();

    // Add event listeners to fields that affect other fields' visibility
    document.querySelectorAll('[data-condition-affects="true"]').forEach(function(group) {
        var select = group.querySelector('select');
        var input = group.querySelector('input');
        var field = select || input;

        if (field) {
            field.addEventListener('change', function() {
                updateConditionalFields();
            });
        }
    });
}

/**
 * Update visibility of all conditional fields based on current form values.
 */
function updateConditionalFields() {
    document.querySelectorAll('[data-condition-field]').forEach(function(group) {
        var conditionField = group.dataset.conditionField;
        var conditionValue = group.dataset.conditionValue;
        var fieldElement = document.querySelector('[name="' + conditionField + '"]');

        if (fieldElement) {
            var shouldShow = fieldElement.value === conditionValue;

            if (shouldShow) {
                showFieldGroup(group);
            } else {
                hideFieldGroup(group);
            }
        }
    });
}

/**
 * Show a field group and restore its required state.
 */
function showFieldGroup(group) {
    group.classList.remove('hidden');
    group.style.display = '';

    // Restore required validation
    var inputs = group.querySelectorAll('input, select, textarea');
    inputs.forEach(function(input) {
        if (input.dataset.wasRequired === 'true') {
            input.required = true;
        }
    });
}

/**
 * Hide a field group and disable its required state.
 */
function hideFieldGroup(group) {
    group.classList.add('hidden');
    group.style.display = 'none';

    // Disable required validation (form will be invalid otherwise)
    var inputs = group.querySelectorAll('input, select, textarea');
    inputs.forEach(function(input) {
        if (input.required) {
            input.dataset.wasRequired = 'true';
            input.required = false;
        }
    });
}

/**
 * Fetch filtered options for a select field.
 * @param {string} selectName - The name of the select field to update
 * @param {string} indexName - The Elasticsearch index to query
 * @param {string} filterField - The field to filter on
 * @param {string} filterValue - The value to filter by
 * @param {string} titleField - The field to use as option title
 */
function loadFilteredOptions(selectName, indexName, filterField, filterValue, titleField) {
    var select = document.querySelector('[name="' + selectName + '"]');
    if (!select) return;

    // Clear existing options
    select.innerHTML = '<option value="">-- Loading... --</option>';

    var url = 'api/options/' + indexName + '/filtered?filter_field=' + filterField + '&filter_value=' + filterValue;

    fetch(url)
    .then(function(response) {
        return response.json();
    })
    .then(function(data) {
        select.innerHTML = '<option value="">-- Select --</option>';
        data.forEach(function(item) {
            var option = document.createElement('option');
            option.value = item.id;
            option.textContent = item.source[titleField] || 'N/A';
            select.appendChild(option);
        });
    })
    .catch(function(error) {
        console.error('Error loading options:', error);
        select.innerHTML = '<option value="">-- Error loading options --</option>';
    });
}
