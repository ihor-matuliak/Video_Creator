import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { ProjectDto } from '../shared/api';
import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { AppStatus, PanelLayout, RememberedTab } from '../shared/bridge';
import { saveViewState } from './bridge-calls';
import { LayoutContext } from './layout-context';
import type { LayoutControl } from './layout-context';
import { ProjectView } from './ProjectView';
import type { TabId } from './ProjectView';
import { ProjectsScreen } from './ProjectsScreen';
import { restoreLastView } from './restore-view';
import type { ImportNotice } from './SourcesTab';
import { TestBadge } from './ui';

const NAV_ITEMS = [
  { label: 'Проєкти', icon: '▦', enabled: true },
  { label: 'Черга', icon: '☰', enabled: false },
  { label: 'Налаштування', icon: '⚙', enabled: false },
] as const;

/** What the main area shows. `restoring`: the last project is being looked up (docs/03 §12). */
type Screen =
  | { readonly kind: 'restoring' }
  | { readonly kind: 'projects' }
  | {
      readonly kind: 'project';
      readonly project: ProjectDto;
      readonly tab: RememberedTab;
      /** A video that could not be registered when the project was created for it (first-run). */
      readonly notice?: ImportNotice | undefined;
    };

/** «Експорт» needs a batch of this session, so a restart returns to «Моменти» instead. */
function rememberedTab(tab: TabId): RememberedTab {
  return tab === 'export' ? 'clips' : tab;
}

/** What the next save writes: the open project with its tab (null: the project list), and the panel widths. */
interface Memory {
  open: { readonly projectId: string; readonly tab: RememberedTab } | null;
  layout: PanelLayout;
}

export function Shell({ status }: { readonly status: AppStatus }) {
  const [collapsed, setCollapsed] = useState(false);
  const [screen, setScreen] = useState<Screen>({ kind: 'restoring' });
  const [layout, setLayout] = useState<PanelLayout>(DEFAULT_LAYOUT);
  const memory = useRef<Memory>({ open: null, layout: DEFAULT_LAYOUT });

  /** Writes what is on screen; every change of project, tab or width goes through here. */
  const remember = useCallback(() => {
    const { open, layout: widths } = memory.current;
    void saveViewState(open === null ? null : { project_id: open.projectId, tab: open.tab, layout: widths });
  }, []);

  const commitLayout = useCallback(
    (next: PanelLayout) => {
      memory.current.layout = next;
      setLayout(next);
      if (memory.current.open !== null) {
        remember();
      }
    },
    [remember],
  );

  const layoutControl = useMemo<LayoutControl>(
    () => ({ layout, commit: commitLayout }),
    [layout, commitLayout],
  );

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the lookup below awaits.
    const run = { cancelled: false };
    void restoreLastView().then((restored) => {
      if (run.cancelled) {
        return;
      }
      if (restored === null) {
        setScreen({ kind: 'projects' });
        return;
      }
      memory.current = {
        open: { projectId: restored.project.id, tab: restored.tab },
        layout: restored.layout,
      };
      setLayout(restored.layout);
      setScreen({ kind: 'project', project: restored.project, tab: restored.tab });
    });
    return () => {
      run.cancelled = true;
    };
  }, []);

  const openProject = useCallback(
    (project: ProjectDto, notice?: ImportNotice) => {
      setScreen({ kind: 'project', project, tab: 'sources', notice });
      memory.current.open = { projectId: project.id, tab: 'sources' };
      remember();
    },
    [remember],
  );

  const showProjects = useCallback(() => {
    setScreen({ kind: 'projects' });
    memory.current.open = null;
    remember();
  }, [remember]);

  return (
    <div className="app" data-testid="app-ready">
      <aside className="sidebar" data-collapsed={collapsed} data-testid="sidebar">
        <nav aria-label="Основна навігація">
          <ul className="nav-list">
            {NAV_ITEMS.map((item) => (
              <li key={item.label}>
                <button
                  type="button"
                  className="nav-item"
                  title={item.label}
                  aria-current={item.enabled ? 'page' : undefined}
                  aria-describedby={item.enabled ? undefined : 'nav-note'}
                  disabled={!item.enabled}
                  onClick={item.enabled ? showProjects : undefined}
                >
                  <span className="nav-icon" aria-hidden="true">
                    {item.icon}
                  </span>
                  <span className="nav-label">{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <p id="nav-note" className="note nav-note">
          <span aria-hidden="true">ⓘ </span>
          Розділи «Черга» і «Налаштування» ще недоступні.
        </p>
        <button
          type="button"
          className="nav-item sidebar-toggle"
          data-testid="sidebar-toggle"
          aria-expanded={!collapsed}
          title={collapsed ? 'Розгорнути панель' : 'Згорнути панель'}
          onClick={() => {
            setCollapsed((value) => !value);
          }}
        >
          <span className="nav-icon" aria-hidden="true">
            {collapsed ? '»' : '«'}
          </span>
          <span className="nav-label">{collapsed ? 'Розгорнути панель' : 'Згорнути панель'}</span>
        </button>
      </aside>
      <div className="content">
        <header className="topbar">
          <h1>ClipForge Local</h1>
          <div className="topbar-status">
            {status.mode === 'test' && <TestBadge />}
            <p className="engine-status" data-testid="engine-status">
              Локальний рушій готовий
              {status.version === null ? '' : ` · версія ${status.version}`}
            </p>
          </div>
        </header>
        <main className="main">
          {screen.kind === 'restoring' && (
            <p role="status" data-testid="restoring" className="note">
              Завантажуємо…
            </p>
          )}
          {screen.kind === 'projects' && <ProjectsScreen onOpen={openProject} />}
          {screen.kind === 'project' && (
            <LayoutContext value={layoutControl}>
              <ProjectView
                key={screen.project.id}
                project={screen.project}
                initialTab={screen.tab}
                importNotice={screen.notice}
                onTabChange={(tab) => {
                  memory.current.open = { projectId: screen.project.id, tab: rememberedTab(tab) };
                  remember();
                }}
                onBack={showProjects}
              />
            </LayoutContext>
          )}
        </main>
      </div>
    </div>
  );
}
