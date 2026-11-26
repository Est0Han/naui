
### Compact panel toolbars

When `api.ui.supports('panel_toolbars')` is true, `api.ui.panel` accepts
`header_rows=[]` and `composer_rows=[]`. Each contains up to eight controls using
the existing panel row schema. A control with `select` is a direct button;
`children`, `menu`, or `actions` opens a bounded popup. Use stable `id` values to
preserve controls while the conversation updates. Empty main `rows` hides the
legacy section entirely. Toolbar callbacks require `commands` permission and
remain guarded by the plugin lifecycle. Open popup inputs and the composer draft
are preserved during message updates; popup contents refresh on reopening.

Toolbar rows without actions or selection render as noninteractive text labels with hover tooltips. Action controls can specify an `icon` (GTK symbolic icon name); `text` remains their accessible label.
