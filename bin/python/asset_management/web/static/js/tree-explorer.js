/**
 * Tree Explorer - Asset Management Navigation System
 * Provides a tree-based navigation interface with details panel
 */

const TreeExplorer = (function() {
    'use strict';

    // ============================================
    // STATE MANAGEMENT
    // ============================================

    const TreeState = {
        expandedCategories: new Set(),
        expandedTypes: new Set(),
        loadedAssets: new Map(),  // Cache: asset_type -> items[]
        selectedNode: null,
        currentAsset: null,
        treeStructure: null,
        treeScrollPosition: 0,
        pageScrollPosition: 0,
        searchQuery: '',
        searchActive: false,

        // Multi-selection tracking
        selectedNodes: new Set(),
        selectionAnchor: null,
        isMultiSelectMode: false,

        // Multi-selection methods
        clearMultiSelection: function() {
            this.selectedNodes.forEach(item => {
                item.node.classList.remove('multi-selected');
            });
            this.selectedNodes.clear();
            this.isMultiSelectMode = false;
            this.selectionAnchor = null;
        },

        addToSelection: function(node, assetType, itemId) {
            node.classList.add('multi-selected');
            this.selectedNodes.add({node, assetType, itemId});
            this.isMultiSelectMode = true;
            this.selectionAnchor = {node, assetType, itemId};
        },

        removeFromSelection: function(node, assetType, itemId) {
            node.classList.remove('multi-selected');
            this.selectedNodes.forEach(item => {
                if (item.itemId === itemId && item.assetType === assetType) {
                    this.selectedNodes.delete(item);
                }
            });
            if (this.selectedNodes.size === 0) {
                this.isMultiSelectMode = false;
            }
        },

        isSelected: function(assetType, itemId) {
            return Array.from(this.selectedNodes).some(
                item => item.assetType === assetType && item.itemId === itemId
            );
        },

        // Save tree state to localStorage
        saveState: function() {
            const state = {
                expandedCategories: Array.from(this.expandedCategories),
                expandedTypes: Array.from(this.expandedTypes),
                currentAsset: this.currentAsset ? {
                    assetType: this.currentAsset.assetType,
                    itemId: this.currentAsset.itemId
                } : null,
                treeScrollPosition: this.treeScrollPosition,
                pageScrollPosition: this.pageScrollPosition
            };
            localStorage.setItem('treeExplorerState', JSON.stringify(state));
        },

        // Load tree state from localStorage
        loadState: function() {
            const saved = localStorage.getItem('treeExplorerState');
            if (saved) {
                try {
                    const state = JSON.parse(saved);
                    this.expandedCategories = new Set(state.expandedCategories || []);
                    this.expandedTypes = new Set(state.expandedTypes || []);
                    this.treeScrollPosition = state.treeScrollPosition || 0;
                    this.pageScrollPosition = state.pageScrollPosition || 0;
                    return state.currentAsset || null;
                } catch (e) {
                    console.error('Failed to load tree state:', e);
                }
            }
            return null;
        },

        // Clear saved state
        clearState: function() {
            localStorage.removeItem('treeExplorerState');
        },

        // Capture current scroll positions from both tree and page
        captureScrollPosition: function() {
            // Capture tree panel scroll
            const treeContent = document.querySelector('.tree-content');
            if (treeContent) {
                this.treeScrollPosition = treeContent.scrollTop;
                console.log('Captured tree scroll position:', this.treeScrollPosition);
            }

            // Capture main page scroll
            this.pageScrollPosition = window.scrollY || window.pageYOffset || document.documentElement.scrollTop;
            console.log('Captured page scroll position:', this.pageScrollPosition);
        },

        // Restore scroll positions to both tree and page
        restoreScrollPosition: function() {
            // Restore tree panel scroll
            const treeContent = document.querySelector('.tree-content');
            if (treeContent && this.treeScrollPosition > 0) {
                console.log('Restoring tree scroll position to:', this.treeScrollPosition);
                treeContent.scrollTop = this.treeScrollPosition;
                console.log('Actual tree scroll position after restore:', treeContent.scrollTop);
            } else {
                console.log('Skip tree scroll restore - position:', this.treeScrollPosition, 'container:', !!treeContent);
            }

            // Restore main page scroll
            if (this.pageScrollPosition > 0) {
                console.log('Restoring page scroll position to:', this.pageScrollPosition);
                window.scrollTo(0, this.pageScrollPosition);
                console.log('Actual page scroll position after restore:', window.scrollY);
            } else {
                console.log('Skip page scroll restore - position:', this.pageScrollPosition);
            }
        }
    };

    // ============================================
    // CONTEXT MENU
    // ============================================

    // the global CSRF hook rejects state-changing POSTs without a
    // token (403 "CSRF validation failed"); every POST in this file must send
    // it. Canonical source is base_v2's <meta name="csrf-token">.
    function csrfToken() {
        const m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.content : '';
    }

    const ContextMenu = {
        element: null,
        _dashboardId: null,

        init: function() {
            this.element = document.createElement('div');
            this.element.className = 'context-menu';
            this.element.innerHTML = `
                <div class="context-menu-item" data-action="convert-to-echarts" style="display:none">
                    <span class="context-menu-icon">⚡</span>
                    <span>Convert to EChart</span>
                </div>
                <div class="context-menu-item" data-action="delete">
                    <span class="context-menu-icon">🗑️</span>
                    <span>Delete Selected</span>
                    <span class="context-menu-count"></span>
                </div>
            `;
            document.body.appendChild(this.element);

            // Click handler
            this.element.addEventListener('click', (e) => {
                const item = e.target.closest('.context-menu-item');
                if (item) {
                    if (item.dataset.action === 'delete') {
                        TreeManager.deleteMultipleAssets();
                    } else if (item.dataset.action === 'convert-to-echarts') {
                        TreeManager.convertKibanaToEChart(ContextMenu._dashboardId);
                    }
                }
                this.hide();
            });

            // Hide on outside click
            document.addEventListener('click', () => this.hide());

            // Hide on Escape
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    this.hide();
                    TreeState.clearMultiSelection();
                }
            });
        },

        show: function(x, y, count, assetType, itemId) {
            this.element.querySelector('.context-menu-count').textContent = `(${count})`;

            // Show "Convert to EChart" only for a single Kibana dashboard item
            const convertItem = this.element.querySelector('[data-action="convert-to-echarts"]');
            if (convertItem) {
                const showConvert = (assetType === 'dashboards' && count === 1);
                convertItem.style.display = showConvert ? '' : 'none';
                this._dashboardId = showConvert ? (itemId || null) : null;
            }

            // Position temporarily off-screen to measure height
            this.element.style.left = '-9999px';
            this.element.style.top = '-9999px';
            this.element.classList.add('visible');

            const menuHeight = this.element.offsetHeight;
            const menuWidth = this.element.offsetWidth;
            const top = (y + menuHeight > window.innerHeight) ? y - menuHeight : y;
            const left = (x + menuWidth > window.innerWidth) ? x - menuWidth : x;
            this.element.style.left = left + 'px';
            this.element.style.top = top + 'px';
        },

        hide: function() {
            this.element.classList.remove('visible');
        }
    };

    // ============================================
    // DOM ELEMENTS
    // ============================================

    let elements = {};

    function initElements() {
        elements = {
            treeRoot: document.getElementById('tree-root'),
            detailsActions: document.getElementById('details-actions'),
            detailsContent: document.getElementById('details-content'),
            loadingOverlay: document.getElementById('loading-overlay'),
            searchInput: document.getElementById('asset-search'),
            clearSearchBtn: document.getElementById('clear-search')
        };
    }

    // ============================================
    // SEARCH MANAGER
    // ============================================

    const SearchManager = {
        /**
         * Convert wildcard pattern (* and ?) to RegExp
         */
        wildcardToRegex: function(pattern) {
            // Escape special regex characters except * and ?
            const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&');
            // Replace * with .* (match any characters) and ? with . (match single character)
            const regexPattern = escaped.replace(/\*/g, '.*').replace(/\?/g, '.');
            return new RegExp(`^${regexPattern}$`, 'i'); // Case-insensitive
        },

        /**
         * Check if a value matches the search pattern
         */
        matches: function(value, pattern) {
            if (!pattern || !value) return false;
            const regex = this.wildcardToRegex(pattern);
            return regex.test(String(value));
        },

        /**
         * Search all loaded assets for matches
         */
        performSearch: async function(query) {
            if (!query || query.trim() === '') {
                this.clearSearch();
                return;
            }

            TreeState.searchQuery = query.trim();
            TreeState.searchActive = true;

            // Show clear button
            elements.clearSearchBtn.classList.add('visible');

            // Add search-active class to tree root
            elements.treeRoot.classList.add('search-active');

            // Clear previous search matches before applying new ones
            document.querySelectorAll('.tree-node.search-match').forEach(node => {
                node.classList.remove('search-match');
            });

            console.log(`Performing search for: "${query}"`);

            try {
                // Call backend search API
                const response = await fetch(`api/search?q=${encodeURIComponent(query)}`);
                const searchResults = await response.json();

                if (searchResults.error) {
                    console.error('Search error:', searchResults.error);
                    return;
                }

                // Track matched item IDs per asset type
                const matchedItems = new Map();

                // Process search results
                for (const [assetKey, assetData] of Object.entries(searchResults.results)) {
                    const itemIds = assetData.matches.map(m => m.id);
                    matchedItems.set(assetKey, new Set(itemIds));
                }

                console.log(`Found matches in ${matchedItems.size} asset types`);

                // asset-type rows no longer expand to a list of
                // items, so we can't pinpoint individual matches in the tree.
                // Instead, expand the containing category and mark the
                // asset-type row itself as a search-match — clicking it
                // navigates to the table view where the matches are visible.
                for (const category of TreeState.treeStructure.categories) {
                    const categoryKey = category.key;
                    let categoryHasMatches = false;

                    for (const assetType of category.asset_types) {
                        if (matchedItems.has(assetType.key)) {
                            categoryHasMatches = true;
                            break;
                        }
                    }

                    if (!categoryHasMatches) continue;

                    // Expand category so the matching asset-type row is visible
                    const categoryNode = document.querySelector(`.tree-node.category-node[data-category="${categoryKey}"]`);
                    if (categoryNode) {
                        const childrenContainer = categoryNode.parentElement.querySelector(`.tree-children[data-category="${categoryKey}"]`);
                        const expandIcon = categoryNode.querySelector('.expand-icon');
                        if (childrenContainer && expandIcon) {
                            childrenContainer.classList.remove('collapsed');
                            expandIcon.classList.add('expanded');
                            TreeState.expandedCategories.add(categoryKey);
                        }
                    }

                    // Mark matching asset-type rows
                    for (const assetType of category.asset_types) {
                        const assetKey = assetType.key;
                        const matchedItemIds = matchedItems.get(assetKey);
                        if (!matchedItemIds || matchedItemIds.size === 0) continue;

                        const typeNode = document.querySelector(`.tree-node.type-node[data-asset-type="${assetKey}"]`);
                        if (typeNode) {
                            typeNode.classList.add('search-match');
                        }
                    }
                }

            } catch (error) {
                console.error('Search failed:', error);
            }
        },

        /**
         * Search within a specific asset for the query
         * This will fetch the asset details and search across all fields
         */
        searchInAsset: function(itemId, assetType, query) {
            // For the simple implementation, we'll just match against the item ID
            // The item ID can be matched using wildcards
            return this.matches(itemId, query);
        },

        /**
         * Clear the search and restore normal view
         */
        clearSearch: function() {
            TreeState.searchQuery = '';
            TreeState.searchActive = false;

            // Hide clear button
            elements.clearSearchBtn.classList.remove('visible');

            // Clear search input
            if (elements.searchInput) {
                elements.searchInput.value = '';
            }

            // Remove search-active class
            elements.treeRoot.classList.remove('search-active');

            // Remove all search-match classes
            document.querySelectorAll('.tree-node.search-match').forEach(node => {
                node.classList.remove('search-match');
            });

            console.log('Search cleared');
        },

        /**
         * Initialize search event handlers
         */
        init: function() {
            if (!elements.searchInput || !elements.clearSearchBtn) {
                console.error('Search elements not found');
                return;
            }

            let searchTimeout = null;

            // Real-time search with debouncing (300ms delay)
            elements.searchInput.addEventListener('input', () => {
                const query = elements.searchInput.value.trim();

                // Show/hide clear button
                if (query !== '') {
                    elements.clearSearchBtn.classList.add('visible');
                } else {
                    elements.clearSearchBtn.classList.remove('visible');
                }

                // Clear previous timeout
                if (searchTimeout) {
                    clearTimeout(searchTimeout);
                }

                // Set new timeout for search
                searchTimeout = setTimeout(() => {
                    this.performSearch(query);
                }, 300);
            });

            // Also search on Enter key (immediate, no delay)
            elements.searchInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    // Clear any pending timeout
                    if (searchTimeout) {
                        clearTimeout(searchTimeout);
                    }
                    const query = elements.searchInput.value.trim();
                    this.performSearch(query);
                }
            });

            // Clear search button
            elements.clearSearchBtn.addEventListener('click', () => {
                this.clearSearch();
            });
        }
    };

    // ============================================
    // TREE MANAGER
    // ============================================

    const TreeManager = {
        init: async function() {
            try {
                const response = await fetch('api/tree/structure');
                const data = await response.json();
                TreeState.treeStructure = data;
                this.renderTree(data);
            } catch (error) {
                console.error('Failed to load tree structure:', error);
                elements.treeRoot.innerHTML = '<div class="error-message">Failed to load assets. Please refresh the page.</div>';
            }
        },

        renderTree: function(data) {
            elements.treeRoot.innerHTML = '';

            data.categories.forEach(category => {
                const categoryContainer = document.createElement('div');
                categoryContainer.className = 'tree-category';

                // Category Node
                const categoryNode = this.createCategoryNode(category);
                categoryContainer.appendChild(categoryNode);

                // Category Children Container
                const categoryChildren = document.createElement('div');
                categoryChildren.className = 'tree-children collapsed';
                categoryChildren.dataset.category = category.key;

                // Asset Type Nodes
                category.asset_types.forEach(assetType => {
                    const typeContainer = this.createAssetTypeNode(assetType, category.key);
                    categoryChildren.appendChild(typeContainer);
                });

                categoryContainer.appendChild(categoryChildren);
                elements.treeRoot.appendChild(categoryContainer);
            });
        },

        createCategoryNode: function(category) {
            const node = document.createElement('div');
            node.className = 'tree-node category-node';
            node.dataset.category = category.key;

            const expandIcon = document.createElement('span');
            expandIcon.className = 'expand-icon';
            expandIcon.textContent = '▶';

            const label = document.createElement('span');
            label.className = 'node-label';
            label.textContent = category.title;

            node.appendChild(expandIcon);
            node.appendChild(label);

            node.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleCategory(node, category.key);
            });

            return node;
        },

        createAssetTypeNode: function(assetType, categoryKey) {
            // asset-type rows no longer expand to list items in the
            // tree. Clicking the row navigates to /asset/<asset_type> (the
            // table view). The "+" button is preserved.
            const container = document.createElement('div');

            // Type Node
            const typeNode = document.createElement('div');
            typeNode.className = 'tree-node type-node';
            typeNode.dataset.assetType = assetType.key;
            typeNode.dataset.category = categoryKey;

            const label = document.createElement('span');
            label.className = 'node-label';
            label.textContent = assetType.title;

            const count = document.createElement('span');
            count.className = 'node-count';
            count.textContent = `(${assetType.count})`;

            typeNode.appendChild(label);
            typeNode.appendChild(count);

            if (!assetType.no_add) {
                const addBtn = document.createElement('button');
                addBtn.className = 'add-btn';
                addBtn.textContent = '+';
                addBtn.title = `Add ${assetType.title}`;
                addBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window.location.href = `asset/${assetType.key}/add?from_tree=true`;
                });
                typeNode.appendChild(addBtn);
            }

            typeNode.addEventListener('click', (e) => {
                e.stopPropagation();
                window.location.href = `asset/${assetType.key}`;
            });

            container.appendChild(typeNode);

            return container;
        },

        toggleCategory: function(node, categoryKey) {
            const childrenContainer = node.parentElement.querySelector(`.tree-children[data-category="${categoryKey}"]`);
            const expandIcon = node.querySelector('.expand-icon');

            if (TreeState.expandedCategories.has(categoryKey)) {
                // Collapse
                childrenContainer.classList.add('collapsed');
                expandIcon.classList.remove('expanded');
                TreeState.expandedCategories.delete(categoryKey);
            } else {
                // Expand
                childrenContainer.classList.remove('collapsed');
                expandIcon.classList.add('expanded');
                TreeState.expandedCategories.add(categoryKey);
            }
            TreeState.captureScrollPosition();
            TreeState.saveState();
        },

        toggleAssetType: async function(node, assetType) {
            const childrenContainer = node.parentElement.querySelector(`.tree-children[data-asset-type="${assetType}"]`);
            const expandIcon = node.querySelector('.expand-icon');

            if (TreeState.expandedTypes.has(assetType)) {
                // Collapse
                childrenContainer.classList.add('collapsed');
                expandIcon.classList.remove('expanded');
                TreeState.expandedTypes.delete(assetType);
            } else {
                // Expand
                expandIcon.classList.add('expanded');
                TreeState.expandedTypes.add(assetType);

                // Load assets if not already loaded
                if (!TreeState.loadedAssets.has(assetType)) {
                    await this.loadAssetItems(assetType, childrenContainer);
                }

                childrenContainer.classList.remove('collapsed');
            }
            TreeState.captureScrollPosition();
            TreeState.saveState();
        },

        loadAssetItems: async function(assetType, container) {
            try {
                // Show loading state
                container.innerHTML = '<div class="tree-loading" style="padding: 10px 0; font-size: 12px;">Loading...</div>';

                const response = await fetch(`api/tree/assets/${assetType}`);
                const data = await response.json();

                if (data.error) {
                    container.innerHTML = '<div class="error-message" style="font-size: 12px;">Failed to load</div>';
                    return;
                }

                // Cache the loaded assets
                TreeState.loadedAssets.set(assetType, data.items);

                // Render asset items
                container.innerHTML = '';

                if (data.items.length === 0) {
                    container.innerHTML = '<div class="empty-state" style="padding: 10px 0; font-size: 12px; color: #999;">No items found</div>';
                    return;
                }

                data.items.forEach(item => {
                    const itemNode = this.createAssetItemNode(item, assetType);
                    container.appendChild(itemNode);
                });

            } catch (error) {
                // pass assetType as a separate arg so any console
                // format specifier in the value (e.g. %o) can't be interpreted
                // as a substitution directive in the message string.
                console.error('Failed to load assets for', assetType, error);
                container.innerHTML = '<div class="error-message" style="font-size: 12px;">Error loading items</div>';
            }
        },

        createAssetItemNode: function(item, assetType) {
            const node = document.createElement('div');
            node.className = 'tree-node item-node';
            node.dataset.assetType = assetType;
            node.dataset.itemId = item.id;

            const icon = document.createElement('span');
            icon.className = 'node-icon';
            icon.textContent = '📄';

            const label = document.createElement('span');
            label.className = 'node-label';
            label.textContent = item.title;

            node.appendChild(icon);
            node.appendChild(label);

            node.addEventListener('click', (e) => {
                e.stopPropagation();
                this.handleItemClick(node, assetType, item.id, e);
            });

            node.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.handleRightClick(node, assetType, item.id, e);
            });

            return node;
        },

        selectAssetItem: function(node, assetType, itemId, skipScrollCapture = false) {
            // Remove previous selection
            if (TreeState.selectedNode) {
                TreeState.selectedNode.classList.remove('selected');
            }

            // Add selection to clicked node
            node.classList.add('selected');
            TreeState.selectedNode = node;

            // Load asset details in details panel
            DetailsPanel.showAssetDetails(assetType, itemId);

            // Save state after selection (including scroll position)
            // Skip capturing scroll when restoring state to avoid overwriting
            if (!skipScrollCapture) {
                TreeState.captureScrollPosition();
            }
            TreeState.saveState();
        },

        handleItemClick: function(node, assetType, itemId, event) {
            if (event.ctrlKey || event.metaKey) {
                // Ctrl+Click: Toggle selection
                if (TreeState.isSelected(assetType, itemId)) {
                    TreeState.removeFromSelection(node, assetType, itemId);
                } else {
                    TreeState.addToSelection(node, assetType, itemId);
                }
                // Show multi-select details
                if (TreeState.selectedNodes.size > 0) {
                    DetailsPanel.showMultiSelectSummary(TreeState.selectedNodes);
                } else {
                    this.selectAssetItem(node, assetType, itemId);
                }
            } else if (event.shiftKey && TreeState.selectionAnchor) {
                // Shift+Click: Range select
                this.selectRange(TreeState.selectionAnchor, {node, assetType, itemId});
                DetailsPanel.showMultiSelectSummary(TreeState.selectedNodes);
            } else {
                // Normal click: Single select
                TreeState.clearMultiSelection();
                this.selectAssetItem(node, assetType, itemId);
            }
        },

        selectRange: function(anchorItem, currentItem) {
            // Must be same asset type
            if (anchorItem.assetType !== currentItem.assetType) {
                return;
            }

            // Get all item nodes for this asset type
            const allNodes = Array.from(document.querySelectorAll(
                `.tree-node.item-node[data-asset-type="${anchorItem.assetType}"]`
            ));

            const anchorIndex = allNodes.indexOf(anchorItem.node);
            const currentIndex = allNodes.indexOf(currentItem.node);

            if (anchorIndex === -1 || currentIndex === -1) return;

            const start = Math.min(anchorIndex, currentIndex);
            const end = Math.max(anchorIndex, currentIndex);

            TreeState.clearMultiSelection();
            for (let i = start; i <= end; i++) {
                const node = allNodes[i];
                const itemId = node.dataset.itemId;
                TreeState.addToSelection(node, anchorItem.assetType, itemId);
            }
        },

        handleRightClick: function(node, assetType, itemId, event) {
            // If right-clicked item not in selection, add it
            if (!TreeState.isSelected(assetType, itemId)) {
                TreeState.clearMultiSelection();
                TreeState.addToSelection(node, assetType, itemId);
            }

            // Show context menu
            if (TreeState.selectedNodes.size > 0) {
                ContextMenu.show(event.clientX, event.clientY, TreeState.selectedNodes.size, assetType, itemId);
            }
        },

        deleteMultipleAssets: async function() {
            const selectedItems = Array.from(TreeState.selectedNodes);

            if (selectedItems.length === 0) return;

            const assetType = selectedItems[0].assetType;

            // Fetch titles for confirmation
            const titles = this.fetchAssetTitles(selectedItems);
            const preview = titles.slice(0, 5).join('\n');
            const more = titles.length > 5 ? `\n... and ${titles.length - 5} more` : '';

            const message = `⚠️ WARNING: You are about to permanently delete ${selectedItems.length} item(s):\n\n${preview}${more}\n\nThis action CANNOT be undone and will remove data from the database.\n\nAre you sure you want to continue?`;

            if (!confirm(message)) {
                return;
            }

            // Capture scroll position before any changes
            TreeState.captureScrollPosition();

            // Find next item to select after deletion
            let nextItem = null;
            const allNodes = Array.from(document.querySelectorAll(
                `.tree-node.item-node[data-asset-type="${assetType}"]`
            ));

            // Get the last selected node in DOM order
            const lastSelectedNode = selectedItems
                .map(item => ({ item, index: allNodes.indexOf(item.node) }))
                .sort((a, b) => b.index - a.index)[0]?.item.node;

            if (lastSelectedNode) {
                // Look for next sibling that's not being deleted
                let candidate = lastSelectedNode.nextElementSibling;
                while (candidate) {
                    if (candidate.classList.contains('item-node') &&
                        !selectedItems.some(item => item.node === candidate)) {
                        nextItem = {
                            assetType: candidate.dataset.assetType,
                            itemId: candidate.dataset.itemId
                        };
                        break;
                    }
                    candidate = candidate.nextElementSibling;
                }

                // If no next item, look for previous sibling
                if (!nextItem) {
                    candidate = lastSelectedNode.previousElementSibling;
                    while (candidate) {
                        if (candidate.classList.contains('item-node') &&
                            !selectedItems.some(item => item.node === candidate)) {
                            nextItem = {
                                assetType: candidate.dataset.assetType,
                                itemId: candidate.dataset.itemId
                            };
                            break;
                        }
                        candidate = candidate.previousElementSibling;
                    }
                }
            }

            try {
                const itemIds = selectedItems.map(item => item.itemId);
                const response = await fetch(`/assets/asset/${assetType}/bulk-delete`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
                    body: JSON.stringify({item_ids: itemIds})
                });

                const result = await response.json();

                if (result.success || result.deleted_count > 0) {
                    // Remove nodes from DOM
                    selectedItems.forEach(item => item.node.remove());

                    // Clear selection
                    TreeState.clearMultiSelection();
                    TreeState.selectedNode = null;

                    // Reload tree to update counts
                    TreeState.loadedAssets.delete(assetType);
                    TreeState.saveState();

                    await TreeManager.init();
                    await TreeManager.restoreTreeState(null);

                    // Select next item or show dashboard
                    if (nextItem) {
                        setTimeout(() => {
                            const itemNode = document.querySelector(
                                `.tree-node.item-node[data-asset-type="${nextItem.assetType}"][data-item-id="${nextItem.itemId}"]`
                            );
                            if (itemNode) {
                                TreeManager.selectAssetItem(itemNode, nextItem.assetType, nextItem.itemId, true);
                            } else {
                                DetailsPanel.showDashboard();
                            }
                        }, 250);
                    } else {
                        await DetailsPanel.showDashboard();
                    }

                    // Show result message
                    if (result.failed_count > 0) {
                        alert(`✓ Deleted ${result.deleted_count} items\n✗ Failed to delete ${result.failed_count} items`);
                    } else {
                        alert(`✓ Successfully deleted ${result.deleted_count} items`);
                    }
                } else {
                    alert('❌ Failed to delete items: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Failed to delete items:', error);
                alert('❌ Network error: Failed to delete items. Please try again.');
            }
        },

        convertKibanaToEChart: async function(dashboardId) {
            if (!dashboardId) return;

            try {
                const resp = await fetch(`api/dashboards/${dashboardId}/convert-to-echarts`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()}
                });
                const data = await resp.json();

                if (data.success) {
                    alert('✓ Dashboard converted! Open it from the dashboards menu.');
                } else {
                    alert('❌ Conversion failed: ' + (data.error || 'Unknown error'));
                }
            } catch (e) {
                console.error('Error converting dashboard:', e);
                alert('❌ Network error during conversion. Please try again.');
            }
        },

        fetchAssetTitles: function(selectedItems) {
            const titles = [];

            for (const item of selectedItems) {
                // Get title from the node's label element
                const labelElement = item.node.querySelector('.node-label');
                const title = labelElement ? labelElement.textContent : 'Untitled';
                titles.push(title);
            }

            // Sort alphabetically (case-insensitive) with numeric sorting
            titles.sort((a, b) => a.localeCompare(b, undefined, {
                sensitivity: 'base',
                numeric: true
            }));

            // Add bullet points
            return titles.map(title => `• ${title}`);
        },

        updateCounts: function(summaryData) {
            // Update counts in tree nodes after delete
            Object.keys(summaryData.by_type).forEach(assetType => {
                const typeNode = document.querySelector(`.tree-node[data-asset-type="${assetType}"]`);
                if (typeNode) {
                    const countSpan = typeNode.querySelector('.node-count');
                    if (countSpan) {
                        countSpan.textContent = `(${summaryData.by_type[assetType].count})`;
                    }
                }
            });
        },

        selectNewAsset: async function(assetType, itemId) {
            console.log(`[selectNewAsset] Attempting to select: ${assetType}/${itemId}`);

            // Find the category for this asset type
            let categoryKey = null;
            for (const cat of TreeState.treeStructure.categories) {
                if (cat.asset_types.some(at => at.key === assetType)) {
                    categoryKey = cat.key;
                    break;
                }
            }

            if (!categoryKey) {
                console.error(`[selectNewAsset] Could not find category for asset type: ${assetType}`);
                return false;
            }

            console.log(`[selectNewAsset] Found category: ${categoryKey}`);

            // Expand the category
            const categoryNode = document.querySelector(`.tree-node.category-node[data-category="${categoryKey}"]`);
            if (categoryNode) {
                const childrenContainer = categoryNode.parentElement.querySelector(`.tree-children[data-category="${categoryKey}"]`);
                const expandIcon = categoryNode.querySelector('.expand-icon');
                if (childrenContainer && expandIcon) {
                    childrenContainer.classList.remove('collapsed');
                    expandIcon.classList.add('expanded');
                    TreeState.expandedCategories.add(categoryKey);
                    console.log(`[selectNewAsset] Expanded category: ${categoryKey}`);
                }
            }

            // Expand the asset type
            const typeNode = document.querySelector(`.tree-node.type-node[data-asset-type="${assetType}"]`);
            if (!typeNode) {
                console.error(`[selectNewAsset] Could not find type node for: ${assetType}`);
                return false;
            }

            const childrenContainer = typeNode.parentElement.querySelector(`.tree-children[data-asset-type="${assetType}"]`);
            const expandIcon = typeNode.querySelector('.expand-icon');

            if (!childrenContainer || !expandIcon) {
                console.error(`[selectNewAsset] Could not find children container or expand icon for: ${assetType}`);
                return false;
            }

            expandIcon.classList.add('expanded');
            TreeState.expandedTypes.add(assetType);

            // Force reload assets to ensure new item is included
            console.log(`[selectNewAsset] Forcing reload of assets for: ${assetType}`);
            TreeState.loadedAssets.delete(assetType);
            await this.loadAssetItems(assetType, childrenContainer);

            childrenContainer.classList.remove('collapsed');

            // Wait for DOM to update then select the item
            setTimeout(() => {
                const itemNode = document.querySelector(
                    `.tree-node.item-node[data-asset-type="${assetType}"][data-item-id="${itemId}"]`
                );
                if (itemNode) {
                    console.log(`[selectNewAsset] Found item node, selecting: ${assetType}/${itemId}`);
                    this.selectAssetItem(itemNode, assetType, itemId);

                    // Scroll the item into view
                    itemNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                } else {
                    console.error(`[selectNewAsset] Could not find item node for ${assetType}/${itemId}`);
                    console.log('[selectNewAsset] All item nodes:', document.querySelectorAll(`.tree-node.item-node[data-asset-type="${assetType}"]`));
                }
            }, 300);

            return true;
        },

        restoreTreeState: async function(savedAsset) {
            // only category expansion is restored. Asset-type rows
            // are now navigation links (they don't expand into the tree), so
            // there's no per-item selection to restore.
            for (const categoryKey of TreeState.expandedCategories) {
                const categoryNode = document.querySelector(`.tree-node.category-node[data-category="${categoryKey}"]`);
                if (categoryNode) {
                    const childrenContainer = categoryNode.parentElement.querySelector(`.tree-children[data-category="${categoryKey}"]`);
                    const expandIcon = categoryNode.querySelector('.expand-icon');
                    if (childrenContainer && expandIcon) {
                        childrenContainer.classList.remove('collapsed');
                        expandIcon.classList.add('expanded');
                    }
                }
            }

            setTimeout(() => {
                TreeState.restoreScrollPosition();
            }, 200);
            return false;
        },

    };

    // ============================================
    // DETAILS PANEL
    // ============================================

    const DetailsPanel = {
        showDashboard: async function() {
            elements.detailsActions.innerHTML = '';
            this.showLoading();

            try {
                const response = await fetch('api/dashboard/summary');
                const data = await response.json();

                this.renderDashboard(data);
                this.hideLoading();
            } catch (error) {
                console.error('Failed to load dashboard:', error);
                this.showError('Failed to load dashboard summary');
                this.hideLoading();
            }
        },

        renderDashboard: function(data) {
            // Asset Type List
            let html = '<div class="card">';
            html += '<h3>Assets by Type</h3>';
            html += '<div class="asset-type-list">';

            Object.keys(data.by_type).forEach(typeKey => {
                const type = data.by_type[typeKey];
                html += '<div class="asset-type-item">';
                html += `<span class="asset-type-name">${escapeHtml(type.title)}</span>`;
                html += `<span class="asset-type-count">${escapeHtml(type.count)}</span>`;
                html += '</div>';
            });

            html += '</div></div>';

            elements.detailsContent.innerHTML = html;
        },

        showAssetDetails: async function(assetType, itemId) {
            this.showLoading();
            elements.detailsActions.innerHTML = '';

            try {
                const response = await fetch(`api/asset/${assetType}/${itemId}/details`);
                const data = await response.json();

                if (data.error) {
                    this.showError(data.error);
                    this.hideLoading();
                    return;
                }

                TreeState.currentAsset = { assetType, itemId, data };
                TreeState.saveState();

                // Render action buttons
                this.renderActionButtons(assetType, itemId, !!(data.asset_config && data.asset_config.code_managed));

                // Render details
                this.renderDetailRows(data);

                this.hideLoading();
            } catch (error) {
                console.error('Failed to load asset details:', error);
                this.showError('Failed to load asset details');
                this.hideLoading();
            }
        },

        showMultiSelectSummary: function(selectedNodes) {
            const count = selectedNodes.size;
            const assetType = Array.from(selectedNodes)[0].assetType;

            elements.detailsActions.innerHTML = '';

            const html = `
                <div class="details-section">
                    <h3 class="section-title">Selection</h3>
                    <div class="summary-card">
                        <div class="summary-stat">
                            <span class="stat-value">${count}</span>
                            <span class="stat-label">items selected</span>
                        </div>
                    </div>
                    <p style="margin-top: 20px; color: #666; font-size: 14px;">
                        Right-click to delete selected items
                    </p>
                </div>
            `;

            elements.detailsContent.innerHTML = html;
        },

        renderActionButtons: function(assetType, itemId, codeManaged) {
            // Clear any existing buttons first to prevent duplicates from double-clicks
            elements.detailsActions.innerHTML = '';

            // a code-managed type (TikTok accounts) is a read-only
            // projection: no Edit / Delete, only the table view.
            if (codeManaged) {
                const tableOnlyBtn = document.createElement('a');
                tableOnlyBtn.href = `asset/${assetType}`;
                tableOnlyBtn.className = 'btn btn-secondary btn-sm';
                tableOnlyBtn.textContent = 'Table View';
                elements.detailsActions.appendChild(tableOnlyBtn);
                return;
            }

            const editBtn = document.createElement('a');
            editBtn.href = `asset/${assetType}/edit/${itemId}?from_tree=true`;
            editBtn.className = 'btn btn-sm';
            editBtn.textContent = 'Edit';

            const tableViewBtn = document.createElement('a');
            tableViewBtn.href = `asset/${assetType}`;
            tableViewBtn.className = 'btn btn-secondary btn-sm';
            tableViewBtn.textContent = 'Table View';

            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'btn btn-danger btn-sm';
            deleteBtn.textContent = 'Delete';
            deleteBtn.addEventListener('click', () => {
                this.deleteAsset(assetType, itemId);
            });

            elements.detailsActions.appendChild(editBtn);
            elements.detailsActions.appendChild(tableViewBtn);
            elements.detailsActions.appendChild(deleteBtn);
        },

        renderDetailRows: function(data) {
            let html = '<div class="card">';

            // Pre-check whether any data source checkbox is enabled AND has a non-empty value
            const hasActiveDataSources = data.asset_config.fields.some(field => {
                if (field.type !== 'data_source_checkbox' || data.data[field.name] !== 'true') return false;
                return data.asset_config.fields.some(controlled => {
                    if (!controlled.condition || controlled.condition.field !== field.name) return false;
                    const val = data.data[controlled.name];
                    if (val === undefined || val === null || val === '') return false;
                    if (Array.isArray(val) && val.length === 0) return false;
                    return true;
                });
            });

            // Render regular fields
            data.asset_config.fields.forEach(field => {
                // Skip UI-only field types
                if (field.type === 'data_source_checkbox') return;

                // Section divider: only show when at least one data source is active
                if (field.type === 'section_divider') {
                    if (hasActiveDataSources) {
                        html += '</div>';
                        html += `<div style="padding:16px 0 8px;"><h3 style="margin:0;font-size:14px;font-weight:700;color:#000716;">${escapeHtml(field.label)}</h3></div>`;
                        html += '<div class="card">';
                    }
                    return;
                }

                // Skip fields with conditions that aren't met
                if (field.condition) {
                    const condFieldGroup = field.condition.field;
                    const condValue = data.data[condFieldGroup];
                    if (condValue !== field.condition.value) {
                        return;
                    }
                    // Also check that the condition field's own group is active
                    // (handles cascade: Google Ads customer hidden when ds_google_ads=false)
                    const condField = data.asset_config.fields.find(f => f.name === condFieldGroup);
                    if (condField && condField.condition) {
                        const parentCondValue = data.data[condField.condition.field];
                        if (parentCondValue !== condField.condition.value) {
                            return;
                        }
                    }
                }

                const value = data.data[field.name];

                // Skip if no value
                if (value === undefined || value === null || value === '') {
                    return;
                }

                // Format value based on type
                // Multi-value fields: always render as boxed items
                const isMultiField = field.type === 'list' || field.type === 'multi_select';

                // Build a flat list of non-empty entries up-front so we can
                // skip rendering the whole row (including label) when every
                // entry is blank — otherwise empty array slots render as
                // empty grey chips.
                let flatItems = null;
                if (Array.isArray(value) && value.length > 0) {
                    flatItems = [];
                    value.forEach(item => {
                        if (item === undefined || item === null) return;
                        const s = String(item);
                        if (s.includes(',')) {
                            s.split(',').forEach(p => { const t = p.trim(); if (t) flatItems.push(t); });
                        } else {
                            const t = s.trim();
                            if (t) flatItems.push(t);
                        }
                    });
                } else if (typeof value === 'string' && value.includes(',')) {
                    flatItems = value.split(',').map(p => p.trim()).filter(p => p);
                } else if (isMultiField) {
                    const t = String(value).trim();
                    flatItems = t ? [t] : [];
                }

                // If a multi-value field flattens to nothing (every entry was
                // empty/whitespace), drop the row entirely.
                if (flatItems !== null && flatItems.length === 0) {
                    return;
                }

                html += '<div class="detail-row">';
                html += `<div class="detail-label">${escapeHtml(field.label)}</div>`;

                if (flatItems !== null) {
                    html += '<div class="detail-value-list">';
                    flatItems.forEach(item => {
                        html += `<div class="detail-value-item">${escapeHtml(item)}</div>`;
                    });
                    html += '</div>';
                } else {
                    html += '<div class="detail-value">';
                    if (field.type === 'url' && value) {
                        html += `<a href="${escapeHtml(value)}" target="_blank">${escapeHtml(value)}</a>`;
                    } else if (field.type === 'logo') {
                        html += `<img src="/static/${escapeHtml(value)}" alt="Organization logo" style="max-height: 80px; max-width: 220px; border: 1px solid #ddd; border-radius: 4px; padding: 4px; background: #fff;">`;
                    } else {
                        html += escapeHtml(value);
                    }
                    html += '</div>';
                }

                html += '</div>';
            });

            // Render computed fields
            if (data.computed_fields && Object.keys(data.computed_fields).length > 0) {
                Object.keys(data.computed_fields).forEach(fieldName => {
                    const fieldData = data.computed_fields[fieldName];
                    html += '<div class="detail-row">';
                    html += `<div class="detail-label">${escapeHtml(fieldData.label)}</div>`;
                    html += '<div class="detail-value">';

                    if (fieldData.type === 'url') {
                        html += `<a href="${escapeHtml(fieldData.value)}" target="_blank">${escapeHtml(fieldData.value)}</a>`;
                    } else {
                        html += escapeHtml(fieldData.value);
                    }

                    html += '</div></div>';
                });
            }

            html += '</div>';
            elements.detailsContent.innerHTML = html;
        },

        deleteAsset: async function(assetType, itemId) {
            const titleField = TreeState.currentAsset.data.asset_config.title_field;
            const title = TreeState.currentAsset.data.data[titleField] || 'this item';

            if (!confirm(`Are you sure you want to delete "${title}"?`)) {
                return;
            }

            this.showLoading();

            try {
                const response = await fetch(`asset/${assetType}/delete/${itemId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrfToken()
                    }
                });

                const result = await response.json();

                if (result.success) {
                    // Capture current scroll position before any changes
                    TreeState.captureScrollPosition();

                    // Find next or previous sibling item before removing the node
                    let nextItem = null;
                    if (TreeState.selectedNode) {
                        const nextSibling = TreeState.selectedNode.nextElementSibling;
                        const prevSibling = TreeState.selectedNode.previousElementSibling;

                        if (nextSibling && nextSibling.classList.contains('item-node')) {
                            nextItem = {
                                node: nextSibling,
                                assetType: nextSibling.dataset.assetType,
                                itemId: nextSibling.dataset.itemId
                            };
                        } else if (prevSibling && prevSibling.classList.contains('item-node')) {
                            nextItem = {
                                node: prevSibling,
                                assetType: prevSibling.dataset.assetType,
                                itemId: prevSibling.dataset.itemId
                            };
                        }
                    }

                    // Remove item from tree DOM
                    if (TreeState.selectedNode) {
                        TreeState.selectedNode.remove();
                        TreeState.selectedNode = null;
                    }

                    // Clear the selected asset from state since it's deleted
                    TreeState.currentAsset = null;

                    // Clear the asset type from cache to force reload
                    TreeState.loadedAssets.delete(assetType);

                    // Save state before reloading tree (preserves expansion and scroll)
                    TreeState.saveState();

                    // Reload tree structure to update counts
                    await TreeManager.init();

                    // Restore tree expansion state and scroll position
                    // Pass null for savedAsset since the deleted item no longer exists
                    await TreeManager.restoreTreeState(null);

                    // Select next/previous item or show dashboard
                    if (nextItem) {
                        // Wait for tree to be fully restored
                        setTimeout(() => {
                            const itemNode = document.querySelector(
                                `.tree-node.item-node[data-asset-type="${nextItem.assetType}"][data-item-id="${nextItem.itemId}"]`
                            );
                            if (itemNode) {
                                TreeManager.selectAssetItem(itemNode, nextItem.assetType, nextItem.itemId, true);
                            } else {
                                this.showDashboard();
                            }
                        }, 250);
                    } else {
                        await this.showDashboard();
                    }
                } else {
                    this.showError(result.error || 'Failed to delete asset');
                }

                this.hideLoading();
            } catch (error) {
                console.error('Failed to delete asset:', error);
                this.showError('Failed to delete asset');
                this.hideLoading();
            }
        },

        showLoading: function() {
            elements.loadingOverlay.classList.remove('hidden');
        },

        hideLoading: function() {
            elements.loadingOverlay.classList.add('hidden');
        },

        showError: function(message) {
            elements.detailsContent.innerHTML = `<div class="error-message">${escapeHtml(message)}</div>`;
        }
    };

    // ============================================
    // PUBLIC API
    // ============================================

    return {
        init: async function() {
            initElements();

            // Initialize context menu
            ContextMenu.init();

            // Initialize search functionality
            SearchManager.init();

            // Initialize tree structure
            await TreeManager.init();

            // Check URL parameters
            const urlParams = new URLSearchParams(window.location.search);
            let assetRestored = false;

            if (urlParams.get('refresh') === 'true') {
                // select_asset / select_item routing no longer
                // applies — newly-added items are now surfaced via the table
                // view at /asset/<type> (app.py redirect), not by selecting a
                // tree node. Just restore category expansion + scroll.
                const savedAsset = TreeState.loadState();
                assetRestored = await TreeManager.restoreTreeState(savedAsset);

                const newUrl = window.location.pathname;
                window.history.replaceState({}, document.title, newUrl);
            } else if (urlParams.get('show') === 'dashboard') {
                // Explicitly showing dashboard - clear saved state and show dashboard
                TreeState.clearState();
                await DetailsPanel.showDashboard();
                assetRestored = true;

                // Clean up URL parameter
                const newUrl = window.location.pathname;
                window.history.replaceState({}, document.title, newUrl);
            } else {
                // Normal page load - restore tree state if available
                const savedAsset = TreeState.loadState();
                if (savedAsset) {
                    assetRestored = await TreeManager.restoreTreeState(savedAsset);
                }
            }

            // Show dashboard if no asset was restored
            if (!assetRestored) {
                await DetailsPanel.showDashboard();
            }
        },

        refresh: async function() {
            // Public method to refresh the tree
            const savedAsset = TreeState.loadState();
            await TreeManager.init();
            const assetRestored = await TreeManager.restoreTreeState(savedAsset);
            if (!assetRestored) {
                await DetailsPanel.showDashboard();
            }
        }
    };
})();
