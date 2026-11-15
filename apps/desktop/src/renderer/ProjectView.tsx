import { useCallback, useMemo, useState } from 'react';

import type { ClipDto, ProjectDto, SourceDto } from '../shared/api';
import type { RememberedTab } from '../shared/bridge';
import { ClipsTab } from './ClipsTab';
import { ExportBar } from './ExportBar';
import { ExportTab } from './ExportTab';
import { summarizeSelection } from './export-logic';
import type { ClipRegistry, ExportRun } from './export-logic';
import { SAVE_STATUS_VIEW } from './save-status';
import type { SaveStatus } from './save-status';
import { SourcesTab } from './SourcesTab';
import type { ImportNotice } from './SourcesTab';
import { StatusText } from './ui';
import { useProjectSources } from './use-project-sources';

export type TabId = RememberedTab | 'export';

const TABS: readonly { readonly id: TabId; readonly label: string }[] = [
  { id: 'sources', label: 'Джерела' },
  { id: 'clips', label: 'Моменти' },
  { id: 'export', label: 'Експорт' },
];

export interface ProjectViewProps {
  readonly project: ProjectDto;
  /** The tab shown first: «Джерела», unless a restart brings the last one back. */
  readonly initialTab?: RememberedTab;
  /** An import that failed before the project opened (first-run); «Джерела» shows it first. */
  readonly importNotice?: ImportNotice | undefined;
  /** Hears every tab the user opens (not the first one), to remember it across restarts. */
  readonly onTabChange?: (tab: TabId) => void;
  readonly onBack: () => void;
}

/**
 * A project: its header and, once the backend has listed the sources registered in it (also
 * those from earlier runs), the Джерела, Моменти and Експорт tabs.
 */
export function ProjectView({
  project,
  initialTab = 'sources',
  importNotice,
  onTabChange,
  onBack,
}: ProjectViewProps) {
  const { load, sources, addSource, refreshSource, reload } = useProjectSources(project.id);
  // What the Моменти tab reports about the autosave of the clip being edited.
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('saved');
  const saveView = SAVE_STATUS_VIEW[saveStatus];

  return (
    <section className="screen" aria-labelledby="project-title">
      <header className="project-header">
        <nav aria-label="Навігаційний ланцюжок">
          <ol className="breadcrumb">
            <li>
              <button type="button" className="link" onClick={onBack}>
                Проєкти
              </button>
            </li>
            <li aria-current="page">{project.name}</li>
          </ol>
        </nav>
        <div className="title-row">
          <h2 id="project-title">{project.name}</h2>
          {/* A live region that is always there, so that a change of state is announced. */}
          <span role="status">
            <StatusText tone={saveView.tone} testId="saved-state">
              {saveView.label}
            </StatusText>
          </span>
        </div>
      </header>

      {load.kind === 'loading' && (
        <p role="status" data-testid="sources-loading" className="note">
          Завантажуємо джерела проєкту…
        </p>
      )}

      {load.kind === 'error' && (
        <div role="alert" data-testid="sources-error" className="notice notice-danger">
          <h3>Не вдалося завантажити джерела проєкту</h3>
          <p>{load.error.message}</p>
          <button type="button" className="button" data-testid="sources-retry" onClick={reload}>
            Спробувати ще раз
          </button>
        </div>
      )}

      {load.kind === 'ready' && (
        <ProjectWorkspace
          project={project}
          sources={sources}
          initialTab={initialTab}
          importNotice={importNotice}
          onTabChange={onTabChange}
          onSourceAdded={addSource}
          onSourceRefreshed={refreshSource}
          onSaveStatusChange={setSaveStatus}
        />
      )}
    </section>
  );
}

const NO_CLIPS: ReadonlySet<string> = new Set();
const NO_KNOWN_CLIPS: ClipRegistry = new Map();

interface ProjectWorkspaceProps {
  readonly project: ProjectDto;
  readonly sources: readonly SourceDto[];
  readonly initialTab: RememberedTab;
  readonly importNotice: ImportNotice | undefined;
  readonly onTabChange: ((tab: TabId) => void) | undefined;
  readonly onSourceAdded: (source: SourceDto) => void;
  readonly onSourceRefreshed: (source: SourceDto) => void;
  readonly onSaveStatusChange: (status: SaveStatus) => void;
}

function ProjectWorkspace({
  project,
  sources,
  initialTab,
  importNotice,
  onTabChange,
  onSourceAdded,
  onSourceRefreshed,
  onSaveStatusChange,
}: ProjectWorkspaceProps) {
  const [tab, setTab] = useState<TabId>(initialTab);
  // Kept here, not in «Джерела»: the tab is rebuilt on every visit, the notice should not be.
  const [notice, setNotice] = useState(importNotice);
  // One source is "current" for both tabs, and the export choice outlives a tab switch.
  const [activeSourceId, setActiveSourceId] = useState<string | null>(null);
  const [startWithForm, setStartWithForm] = useState(false);
  const [selectedClipIds, setSelectedClipIds] = useState<ReadonlySet<string>>(NO_CLIPS);
  // Every clip the Моменти tab has listed: the export bar needs the review status of ticked
  // clips of any source, and the batch view their titles.
  const [knownClips, setKnownClips] = useState<ClipRegistry>(NO_KNOWN_CLIPS);
  const [reviewFocusClipId, setReviewFocusClipId] = useState<string | null>(null);
  // The latest export batch of this project; the Експорт tab exists once there is one.
  const [exportRun, setExportRun] = useState<ExportRun | null>(null);

  const selection = useMemo(
    () => summarizeSelection(selectedClipIds, knownClips),
    [selectedClipIds, knownClips],
  );

  const rememberClips = useCallback((clips: readonly ClipDto[]) => {
    setKnownClips((current) => {
      const next = new Map(current);
      for (const clip of clips) {
        next.set(clip.id, clip);
      }
      return next;
    });
  }, []);

  const clearReviewFocus = useCallback(() => {
    setReviewFocusClipId(null);
  }, []);

  // Every change of tab goes through here, so that each one is remembered for the next start.
  const selectTab = (next: TabId): void => {
    setTab(next);
    onTabChange?.(next);
  };

  const openTab = (next: TabId): void => {
    setStartWithForm(false);
    setReviewFocusClipId(null);
    selectTab(next);
  };

  const showRun = (run: ExportRun): void => {
    setExportRun(run);
    openTab('export');
  };

  const reviewSkipped = (): void => {
    const [first] = selection.skipped;
    if (first === undefined) {
      return;
    }
    if (first.sourceId !== null) {
      setActiveSourceId(first.sourceId);
    }
    setReviewFocusClipId(first.id);
  };

  const exportEnabled = exportRun !== null;

  return (
    <>
      <div role="tablist" aria-label="Розділи проєкту" className="tabs">
        {TABS.map((item) => {
          const enabled = item.id !== 'export' || exportEnabled;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              className="tab"
              id={`tab-${item.id}`}
              aria-selected={item.id === tab}
              aria-controls={enabled ? `panel-${item.id}` : undefined}
              aria-describedby={enabled ? undefined : 'tabs-note'}
              disabled={!enabled}
              onClick={() => {
                openTab(item.id);
              }}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      {!exportEnabled && (
        <p id="tabs-note" className="note">
          <span aria-hidden="true">ⓘ </span>
          Розділ «Експорт» ще недоступний.
        </p>
      )}

      {tab === 'sources' && (
        <div role="tabpanel" id="panel-sources" aria-labelledby="tab-sources">
          <SourcesTab
            project={project}
            sources={sources}
            notice={notice}
            onNoticeHandled={() => {
              setNotice(undefined);
            }}
            selectedSourceId={activeSourceId}
            onSelectSource={setActiveSourceId}
            onSourceAdded={onSourceAdded}
            onSourceRefreshed={onSourceRefreshed}
            onManualClip={(sourceId) => {
              setActiveSourceId(sourceId);
              setStartWithForm(true);
              selectTab('clips');
            }}
          />
        </div>
      )}
      {tab === 'clips' && (
        <div role="tabpanel" id="panel-clips" aria-labelledby="tab-clips">
          <ClipsTab
            sources={sources}
            activeSourceId={activeSourceId}
            onSourceChange={setActiveSourceId}
            startWithForm={startWithForm}
            selectedClipIds={selectedClipIds}
            onSelectedClipIdsChange={setSelectedClipIds}
            onGoToSources={() => {
              openTab('sources');
            }}
            onClipsKnown={rememberClips}
            reviewFocusClipId={reviewFocusClipId}
            onReviewFocusHandled={clearReviewFocus}
            onSaveStatusChange={onSaveStatusChange}
          />
          {sources.length > 0 && (
            <ExportBar
              projectId={project.id}
              sources={sources}
              selection={selection}
              onCreated={showRun}
              onReviewSkipped={reviewSkipped}
            />
          )}
        </div>
      )}
      {tab === 'export' && exportRun !== null && (
        <div role="tabpanel" id="panel-export" aria-labelledby="tab-export">
          <ExportTab key={exportRun.id} run={exportRun} clips={knownClips} onRetried={showRun} />
        </div>
      )}
    </>
  );
}
