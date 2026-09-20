#!/usr/bin/env node
/**
 * shadcn/ui 组件同步脚本 —— 官方 CLI 的降级替代方案。
 *
 * 为什么存在这个脚本：
 *   `npx shadcn@latest` 的依赖树中包含 socks -> smart-buffer，该包的包元数据在当前
 *   网络内容策略下被拦截（CODEBUDDY_BROKER_DENY），导致官方 CLI 无法安装运行。
 *   本脚本改为直接通过 HTTPS 从 shadcn 官方 registry 取组件源码，效果等价：
 *   registry 返回的 dependencies / registryDependencies / files 结构与 CLI 使用的完全一致。
 *
 * 本脚本复刻了官方 CLI 在写入前做的三件事：
 *   1. 路径映射：`registry/<style>/ui/x.tsx` -> `src/components/ui/x.tsx`
 *   2. import 重写：`@/registry/<style>/ui/x` -> `@/components/ui/x`（含 lib / hooks）
 *   3. 依赖收集：合并所有条目的 dependencies 与 registryDependencies（递归）
 *
 * 注意 style 命名空间有两套：
 *   - `new-york-v4`：Tailwind v4 版本，组件用 `radix-ui` 统一包与 `cn` 包。**本脚本使用这套。**
 *   - `new-york`：Tailwind v3 遗留版本，带 `tailwindcss-animate` 与 `tailwind.config`，
 *     且使用 `text-destructive-foreground` 这类 neutral 主题并不提供的 token。
 * 混用会导致样式静默失效，务必保持 `new-york-v4`。
 *
 * 用法：
 *   node scripts/sync-shadcn.mjs            # 同步下方 COMPONENTS 列表
 *   node scripts/sync-shadcn.mjs button card
 *
 * 本脚本只覆盖写入，不做清理：若切换了 style 命名空间，请先删除
 * src/components/ui、src/hooks、src/lib 再运行，避免新旧文件混杂。
 *
 * 若日后官方 CLI 可用，优先使用 `npx shadcn@latest add <name>`，本脚本可作为离线兜底。
 */

import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const REGISTRY = process.env.SHADCN_REGISTRY ?? 'https://ui.shadcn.com/r'

/** registry 路径中的 style 命名空间。Tailwind v4 对应 `new-york-v4`。 */
const REGISTRY_STYLE = process.env.SHADCN_REGISTRY_STYLE ?? 'new-york-v4'

/** 写进 components.json 的 style 值。shadcn CLI 在 v4 下仍写 `new-york`。 */
const CONFIG_STYLE = 'new-york'

const THEME = process.env.SHADCN_THEME ?? 'neutral'
const ROOT = path.resolve(import.meta.dirname, '..')
const REGISTRY_PREFIX = `registry/${REGISTRY_STYLE}/`

/** 计划 §10 组件映射表所需的组件。 */
const COMPONENTS = [
  'button', 'card', 'input', 'badge', 'dialog', 'alert-dialog', 'sheet',
  'tabs', 'table', 'progress', 'separator', 'scroll-area', 'tooltip',
  'skeleton', 'select', 'checkbox', 'switch', 'label', 'form', 'sonner',
  'collapsible', 'hover-card', 'command', 'toggle-group', 'sidebar',
  'resizable', 'breadcrumb', 'dropdown-menu',
]

/**
 * v3 时代的 Tailwind 插件，在 v4 下由 tw-animate-css 取代。
 * 若照搬进 package.json 会引入一个无效且与 v4 冲突的依赖。
 */
const LEGACY_NPM_DEPS = new Set(['tailwindcss-animate'])

async function fetchJson(url) {
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`GET ${url} -> HTTP ${response.status}`)
  }
  return response.json()
}

/** 去掉 `pkg@^1` 里的版本后缀，保留 `@scope/pkg` 的 scope 前缀。 */
function normalizeNpmDep(spec) {
  const at = spec.lastIndexOf('@')
  return at > 0 ? spec.slice(0, at) : spec
}

/** 把 registry 的相对路径映射到 src/ 下的实际位置。 */
function targetPath(relative) {
  const rel = relative.startsWith(REGISTRY_PREFIX)
    ? relative.slice(REGISTRY_PREFIX.length)
    : relative
  const [head, ...rest] = rel.split('/')
  const base = {
    ui: 'src/components/ui',
    lib: 'src/lib',
    hooks: 'src/hooks',
    components: 'src/components',
  }[head] ?? `src/${head}`
  return path.join(ROOT, base, rest.join('/'))
}

/** 把 registry 内部的绝对别名重写成项目的 `@/` 别名。 */
function rewriteImports(content) {
  return content
    .replaceAll(`@/registry/${REGISTRY_STYLE}/ui/`, '@/components/ui/')
    .replaceAll(`@/registry/${REGISTRY_STYLE}/lib/`, '@/lib/')
    .replaceAll(`@/registry/${REGISTRY_STYLE}/hooks/`, '@/hooks/')
    .replaceAll(`@/registry/${REGISTRY_STYLE}/components/`, '@/components/')
}

/** 递归收集组件及其 registryDependencies。 */
async function collect(name, seen, files, npmDeps) {
  if (seen.has(name)) return
  seen.add(name)

  const item = await fetchJson(`${REGISTRY}/styles/${REGISTRY_STYLE}/${name}.json`)
  for (const dependency of item.registryDependencies ?? []) {
    const dep = dependency.includes('/')
      ? dependency.split('/').pop().replace(/\.json$/, '')
      : dependency
    await collect(dep, seen, files, npmDeps)
  }
  for (const raw of item.dependencies ?? []) {
    const dependency = normalizeNpmDep(raw)
    if (!LEGACY_NPM_DEPS.has(dependency)) {
      npmDeps.add(dependency)
    }
  }
  for (const file of item.files ?? []) {
    files.set(targetPath(file.path), rewriteImports(file.content))
  }
}

/**
 * 生成 Tailwind v4 版 index.css。
 *
 * 数据来源必须是 `registry/colors/{theme}.json` 的 `cssVarsV4` 字段：
 *   - 颜色是完整的 oklch(...) 值，可直接作为 CSS 自定义属性使用；
 *   - `registry/themes/{theme}.json` 的 `cssVars` 是 v3 时代的 HSL 三元组
 *     （如 `0 0% 100%`），缺 `hsl(...)` 外壳，在 v4 下不会产生任何颜色。
 *
 * `--radius` 只存在于 light 分组，因此 :root 与 .dark 的变量集合要分别取值。
 */
function buildIndexCss(cssVars) {
  const light = cssVars.light ?? {}
  const dark = cssVars.dark ?? {}
  const colorNames = [...new Set([...Object.keys(light), ...Object.keys(dark)])]
    .filter((n) => n !== 'radius')

  const block = (vars, selector) => {
    const body = Object.keys(vars)
      .map((n) => `  --${n}: ${vars[n]};`)
      .join('\n')
    return `${selector} {\n${body}\n}`
  }

  const colorTokens = colorNames.map((n) => `  --color-${n}: var(--${n});`).join('\n')

  const radiusTokens = Object.hasOwn(light, 'radius')
    ? [
        '  --radius-sm: calc(var(--radius) - 4px);',
        '  --radius-md: calc(var(--radius) - 2px);',
        '  --radius-lg: var(--radius);',
        '  --radius-xl: calc(var(--radius) + 4px);',
      ].join('\n')
    : ''

  return `@import "tailwindcss";
@import "tw-animate-css";

@custom-variant dark (&:is(.dark *));

${block(light, ':root')}

${block(dark, '.dark')}

@theme inline {
${colorTokens}
${radiusTokens}
}

@layer base {
  * {
    @apply border-border outline-ring/50;
  }
  body {
    @apply bg-background text-foreground;
  }
}
`
}

const COMPONENTS_JSON = {
  $schema: 'https://ui.shadcn.com/schema.json',
  style: CONFIG_STYLE,
  rsc: false,
  tsx: true,
  tailwind: { config: '', css: 'src/index.css', baseColor: THEME, cssVariables: true },
  aliases: {
    components: '@/components',
    utils: '@/lib/utils',
    ui: '@/components/ui',
    lib: '@/lib',
    hooks: '@/hooks',
  },
  iconLibrary: 'lucide',
}

async function main() {
  const requested = process.argv.slice(2)
  const names = requested.length > 0 ? requested : COMPONENTS

  const files = new Map()
  const npmDeps = new Set()
  const seen = new Set()

  for (const name of names) {
    process.stdout.write(`拉取 ${name} ... `)
    await collect(name, seen, files, npmDeps)
    process.stdout.write('ok\n')
  }

  const styleIndex = await fetchJson(`${REGISTRY}/styles/${REGISTRY_STYLE}/index.json`)
  for (const dependency of styleIndex.registryDependencies ?? []) {
    const dep = dependency.includes('/')
      ? dependency.split('/').pop().replace(/\.json$/, '')
      : dependency
    await collect(dep, seen, files, npmDeps)
  }
  for (const raw of styleIndex.dependencies ?? []) {
    const dependency = normalizeNpmDep(raw)
    if (!LEGACY_NPM_DEPS.has(dependency)) {
      npmDeps.add(dependency)
    }
  }

  const colors = await fetchJson(`${REGISTRY}/colors/${THEME}.json`)
  const cssVars = colors.cssVarsV4
  if (!cssVars) {
    throw new Error(`colors/${THEME}.json 缺少 cssVarsV4，无法生成 Tailwind v4 主题变量`)
  }

  files.set(path.join(ROOT, 'src/index.css'), buildIndexCss(cssVars))
  files.set(
    path.join(ROOT, 'components.json'),
    `${JSON.stringify(COMPONENTS_JSON, null, 2)}\n`,
  )

  for (const [target, content] of files) {
    await mkdir(path.dirname(target), { recursive: true })
    await writeFile(target, content, 'utf8')
  }

  console.log(`\n已写入 ${files.size} 个文件，registry 条目 ${seen.size} 个。`)
  console.log(`\n需要安装的 npm 依赖：\n  npm install ${[...npmDeps].sort().join(' ')}`)
}

await main()
