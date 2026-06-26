import PluginApp from './PluginApp'

// Explicit global assignment — more reliable than relying on Rollup's IIFE
// named-export → window property mechanism in strict-mode bundles.
;(window as unknown as Record<string, unknown>)['__lyndrix_plugin_lyndrix_plugin_bingo'] = {
  PluginApp,
  pluginRoutes: [
    { path: '/bingo', label: 'Meeting Bingo', icon: 'grid_on', sidebar_visible: true },
  ],
}
