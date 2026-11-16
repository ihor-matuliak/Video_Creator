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
import { SettingsScreen } from './SettingsScreen';
import { OpenSettingsContext } from './settings-nav';
import type { ImportNotice } from './SourcesTab';
import { TestBadge } from './ui';
import { ViewActiveContext } from './view-active';

const NAV_ITEMS = [
  { id: 'projects', label: 'Проєкти', icon: '▦', enabled: true },
  { id: 'queue', label: 'Черга', icon: '☰', enabled: false },
  { id: 'settings', label: 'Налаштування', icon: '⚙', enabled: true },
] as const;

/**
 * `main` is the projects area (list or open project); `settings` takes its place on screen. An open
 * project is not closed by that: it stays mounted but hidden, so it comes back with its tab, its
 * export run and its clip selection (and stops playing and listening for shortcuts meanwhile). The
 * project list and the loading note are not kept; the list reads its data again.
 */
type Section = 'main' | 'settings';

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
  const [section, setSection] = useState<Section>('main');
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

  /** For what is shown inside a project (the speech-recognition block): the same as the sidebar item. */
  const openSettings = useCallback(() => {
    setSection('settings');
  }, []);

  const showProjects = useCallback(() => {
    setSection('main');
    setScreen({ kind: 'projects' });
    memory.current.open = null;
    remember();
  }, [remember]);

  /**
   * «Проєкти» from the settings goes back to what was open before them, and from anywhere else
   * shows the project list. Opening the settings changes nothing that is remembered.
   */
  const navigate = (id: (typeof NAV_ITEMS)[number]['id']): void => {
    if (id === 'settings') {
      setSection('settings');
    } else if (id === 'projects') {
      if (section === 'settings') {
        setSection('main');
      } else {
        showProjects();
      }
    }
  };

  return (
    <div className="app" data-testid="app-ready">
      <aside className="sidebar" data-collapsed={collapsed} data-testid="sidebar">
        <nav aria-label="Основна навігація">
          <ul className="nav-list">
            {NAV_ITEMS.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className="nav-item"
                  title={item.label}
                  aria-current={
                    item.enabled && (item.id === 'settings') === (section === 'settings')
                      ? 'page'
                      : undefined
                  }
                  aria-describedby={item.enabled ? undefined : 'nav-note'}
                  disabled={!item.enabled}
                  onClick={
                    item.enabled
                      ? () => {
                          navigate(item.id);
                        }
                      : undefined
                  }
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
          Розділ «Черга» ще недоступний.
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
          {section === 'settings' && <SettingsScreen />}
          {section === 'main' && screen.kind === 'restoring' && (
            <p role="status" data-testid="restoring" className="note">
              Завантажуємо…
            </p>
          )}
          {section === 'main' && screen.kind === 'projects' && <ProjectsScreen onOpen={openProject} />}
          {screen.kind === 'project' && (
            <div data-testid="project-pane" hidden={section === 'settings'}>
              <ViewActiveContext value={section === 'main'}>
                <OpenSettingsContext value={openSettings}>
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
                </OpenSettingsContext>
              </ViewActiveContext>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
