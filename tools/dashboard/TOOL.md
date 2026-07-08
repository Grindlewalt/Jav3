---
name: dashboard
description: Create an interactive HTML dashboard in the active project. Writes a single self-contained .html file under dashboards/ (staged for operator approval); once approved it renders live in the project workspace Renderer panel.
when_to_use: When the operator asks for a dashboard, chart, visualization, or any interactive HTML view of project data.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Relative file path ending in .html (e.g. "metrics.html"). No leading slash, no "..". Automatically placed under dashboards/ if not already there.
    html:
      type: string
      description: The complete, self-contained HTML document.
  required: [path, html]
---
Write ONE self-contained HTML file: inline `<style>` and `<script>` only. The
Renderer panel shows it in a sandboxed iframe (scripts allowed, but NO network
and no same-origin access), so external CDNs, stylesheets, fonts, fetch() and
API calls will NOT work — embed data as inline JSON and draw charts with
`<canvas>` or inline SVG. Keep it responsive (relative units, max-width). The
dashboard appears in the workspace Renderer panel only AFTER the operator
approves the staged file.
