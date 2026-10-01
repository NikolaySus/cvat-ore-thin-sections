import React, { useEffect, useRef, useState } from '@modules/react';
import { createRoot, Root } from '@modules/react-dom/client';
import {
    CloseOutlined, PlayCircleOutlined, EyeOutlined, EditOutlined, LinkOutlined,
    UndoOutlined, DownloadOutlined, UploadOutlined, ZoomInOutlined, ZoomOutOutlined,
    AimOutlined, DeleteOutlined, StopOutlined, DragOutlined,
} from '@modules/@ant-design/icons';
import { createAnnotationsAsync, fetchAnnotationsAsync } from 'actions/annotation-actions';
import { ObjectType, ShapeType, Source } from 'cvat-core-wrapper';
import './style.css';

type Point = [number, number];
const API = '/ore/api';
const classes = [
    { key: 'matrix', title: 'Матрица', color: '#ffeb00' },
    { key: 'ore', title: 'Сульфидные фазы', color: '#ff2323' },
    { key: 'talc', title: 'Тальк', color: '#0046ff' },
    { key: 'damage', title: 'Дефекты', color: '#80002d' },
];
const names: Record<string, string> = {
    k: 'Кластеры', ore_l_min: 'Минимальная светлота руды', ore_b_min: 'Минимальная желтизна руды',
    talc_l_max: 'Максимальная светлота талька', matrix_subclusters: 'Подкластеры матрицы',
    ore_boundary_shift: 'Граница руды', talc_dark_quantile: 'Квантиль талька',
    min_component_area: 'Минимальная площадь объекта', morph_radius: 'Радиус морфологии',
    max_work_side: 'Рабочий размер', sample_pixels: 'Пикселей для кластеризации',
    edge_refinement_strength: 'Уточнение границ', edge_gradient_radius: 'Радиус градиента',
    edge_seed_erosion_radius: 'Эрозия семян watershed', edge_transition_radius: 'Зона перехода',
    edge_soft_delta_e: 'Мягкий переход ΔE', edge_min_region_area: 'Минимальная площадь региона',
    illumination_correction_strength: 'Коррекция освещения', illumination_ore_erosion_radius: 'Эрозия руды',
    illumination_min_ore_area: 'Минимальная площадь руды', illumination_outlier_quantile: 'Выбросы руды',
    illumination_max_delta_l: 'Максимальная коррекция ΔL', local_shadow_correction_strength: 'Коррекция теней',
    local_shadow_radius: 'Радиус теней', local_shadow_max_delta_l: 'Максимальная тень ΔL',
    local_shadow_chroma_barrier: 'Барьер оттенка', local_shadow_lightness_barrier: 'Барьер светлоты',
    endpoint_min_branch_length: 'Минимальная длина конца', connection_max_distance: 'Расстояние соединения',
    connection_neighbors_per_endpoint: 'Соседи по расстоянию', angular_neighbors_per_endpoint: 'Угловые соседи',
    crop_excluded_talc_max_fraction: 'Допустимый лишний тальк', crop_min_area_ratio: 'Минимальная площадь области',
    crop_max_rotation_degrees: 'Максимальный поворот области', crop_sketch_erosion_radius: 'Подрезка эскиза',
};

async function request(path: string, options: RequestInit = {}): Promise<any> {
    const response = await fetch(API + path, { credentials: 'same-origin', ...options });
    const text = await response.text();
    let data;
    try { data = JSON.parse(text); } catch { throw new Error(`Сервис недоступен (${response.status}).`); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Ошибка запроса (${response.status}).`);
    return data;
}

function download(name: string, data: string, type = 'application/json'): void {
    const url = URL.createObjectURL(new Blob([data], { type }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function curve(segment: any): string {
    const { a, b, control: c, tangent: t } = segment;
    const sum = [(8 * c[0] - a[0] - b[0]) / 3, (8 * c[1] - a[1] - b[1]) / 3];
    const delta = [4 * t[0] / 3 - (b[0] - a[0]), 4 * t[1] / 3 - (b[1] - a[1])];
    const first = [(sum[0] - delta[0]) / 2, (sum[1] - delta[1]) / 2];
    const second = [(sum[0] + delta[0]) / 2, (sum[1] + delta[1]) / 2];
    return `M ${a.join(' ')} C ${first.join(' ')} ${second.join(' ')} ${b.join(' ')}`;
}

function Panel({ context, close }: { context: any; close: () => void }): JSX.Element {
    const { store, core, dispatch } = context;
    const job = store.getState().annotation.job.instance;
    const frame = store.getState().annotation.player.frame.number;
    const base = `/frames/${job.id}/${frame}`;
    const [record, setRecord] = useState<any>(null);
    const [settings, setSettings] = useState<any>(null);
    const [geometry, setGeometry] = useState<any>(null);
    const pendingStrokes = settings?.strokes.slice(geometry?.processedStrokeCount || 0) || [];
    const geometryPending = Boolean(geometry && pendingStrokes.length);
    const [result, setResult] = useState<any>(null);
    const [work, setWork] = useState<any>(null);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [view, setView] = useState('sketch');
    const [mode, setMode] = useState('pan');
    const [opacity, setOpacity] = useState(.55);
    const [showSketch, setShowSketch] = useState(false);
    const [selection, setSelection] = useState<any>(null);
    const [selectedSegment, setSelectedSegment] = useState<string | null>(null);
    const [box, setBox] = useState<number[]>([0, 0, 1, 1]);
    const [mapping, setMapping] = useState<Record<string, number>>({});
    const [busy, setBusy] = useState(false);
    const [applying, setApplying] = useState(false);
    const [applied, setApplied] = useState<string[]>([]);
    const [draft, setDraft] = useState<Point[]>([]);
    const svgRef = useRef<SVGSVGElement>(null);
    const drag = useRef<any>(null);
    const history = useRef<any[]>([]);
    const activeWork = useRef<string | null>(null);
    const mounted = useRef(true);
    const upload = useRef<HTMLInputElement>(null);
    const importSettings = useRef<HTMLInputElement>(null);
    const currentSettings = useRef<any>(null);
    currentSettings.current = settings;

    useEffect(() => {
        request(base).then((data) => {
            if (!mounted.current) return;
            setRecord(data);
            setSettings(data.state);
            setBox([0, 0, data.width, data.height]);
            const next: Record<string, number> = {};
            for (const item of classes) {
                const label = job.labels.find((candidate: any) => candidate.name === item.title || candidate.name === item.key);
                if (label) next[item.key] = label.id;
            }
            setMapping(next);
        }).catch((exc) => setError(exc.message));
        const unsubscribe = store.subscribe(() => {
            const current = store.getState().annotation;
            if (current.job.instance?.id !== job.id || current.player.frame.number !== frame) close();
        });
        return () => {
            mounted.current = false;
            unsubscribe();
            if (activeWork.current) request(`/jobs/${activeWork.current}`, { method: 'DELETE' }).catch(() => {});
        };
    }, []);

    function change(next: any): void {
        history.current.push(structuredClone(settings));
        history.current = history.current.slice(-30);
        setSettings(next);
        setResult(null);
        setNotice('');
    }

    async function save(): Promise<any> {
        const data = await request(base, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ revision: record.revision, state: currentSettings.current }) });
        setRecord({ ...record, ...data });
        return data;
    }

    async function closeSaved(): Promise<void> {
        if (!settings || busy) { close(); return; }
        try {
            if (JSON.stringify(settings) !== JSON.stringify(record.state)) await save();
            close();
        } catch (exc) { setError(exc.message); }
    }

    async function calculate(kind: string): Promise<boolean> {
        if (kind === 'segmentation' && showSketch && (!geometry || geometry.sourceKey !== geometryKey(settings))) {
            if (!await calculate('geometry')) return false;
        }
        setBusy(true);
        setError('');
        setNotice('');
        try {
            const saved = await save();
            const started = await request(`${base}/jobs?revision=${saved.revision}&kind=${kind}`, { method: 'POST' });
            activeWork.current = started.id;
            setWork(started);
            let status = started;
            while (mounted.current && ['queued', 'running'].includes(status.status)) {
                await new Promise((resolve) => setTimeout(resolve, 800));
                if (!mounted.current) return false;
                status = await request(`/jobs/${started.id}`);
                setWork(status);
            }
            activeWork.current = null;
            if (!mounted.current) return false;
            if (status.status === 'failed') throw new Error(status.error);
            if (status.status === 'completed') {
                const data = await request(`/jobs/${started.id}/result`);
                if (kind === 'geometry') {
                    setGeometry({ ...data.result, processedStrokeCount: saved.state.strokes.length,
                        sourceKey: geometryKey(saved.state) });
                    setSelectedSegment(null);
                    setSelection(null);
                } else {
                    setResult({ ...data, id: started.id });
                    setView('result');
                    setMode('pan');
                    if (settings.corrected && !data.result.correction_applied) setNotice('Замкнутая область не найдена. Коррекция по эскизу не применена.');
                }
                return true;
            }
        } catch (exc) { if (mounted.current) setError(exc.message); }
        finally { if (mounted.current) setBusy(false); }
        return false;
    }

    function geometryKey(state: any): string {
        return JSON.stringify([state.sketch, state.strokes, state.segments, state.regionMode]);
    }

    async function toggleSketch(checked: boolean): Promise<void> {
        if (!checked) { setShowSketch(false); return; }
        if ((!geometry || geometry.sourceKey !== geometryKey(settings)) && !await calculate('geometry')) return;
        setShowSketch(true);
    }

    function point(event: React.PointerEvent | React.WheelEvent): Point {
        const value = svgRef.current.createSVGPoint();
        value.x = event.clientX;
        value.y = event.clientY;
        const transformed = value.matrixTransform(svgRef.current.getScreenCTM().inverse());
        return [transformed.x, transformed.y];
    }

    function pointerDown(event: React.PointerEvent): void {
        if (busy) return;
        const p = point(event);
        if (mode === 'draw' && view === 'sketch') {
            setDraft([p]);
            drag.current = { type: 'stroke', points: [p] };
        } else if (!drag.current && mode === 'pan') {
            drag.current = { type: 'pan', start: p, box: [...box] };
        }
        if (drag.current) svgRef.current.setPointerCapture(event.pointerId);
    }

    function pointerMove(event: React.PointerEvent): void {
        if (!drag.current) return;
        const p = point(event);
        const active = drag.current;
        if (active.type === 'stroke') {
            active.points.push(p);
            setDraft([...active.points]);
        } else if (active.type === 'pan') {
            setBox([box[0] + active.start[0] - p[0], box[1] + active.start[1] - p[1], box[2], box[3]]);
        } else {
            const next = structuredClone(currentSettings.current);
            const segment = next.segments.find((item: any) => item.id === active.id);
            if (active.type === 'control') segment.control = p;
            else segment.tangent = [p[0] - segment.control[0], p[1] - segment.control[1]];
            setSettings(next);
            setResult(null);
        }
    }

    function pointerUp(): void {
        const active = drag.current;
        drag.current = null;
        if (active?.type === 'stroke' && active.points.length > 1) {
            change({ ...settings, strokes: [...settings.strokes, active.points] });
            setSelection(null);
            setSelectedSegment(null);
        }
        setDraft([]);
    }

    function connectEndpoint(endpoint: any): void {
        if (busy || geometryPending) return;
        if (!selection) { setSelection(endpoint); return; }
        if (selection.id === endpoint.id) { setSelection(null); return; }
        const a: Point = [selection.x, selection.y];
        const b: Point = [endpoint.x, endpoint.y];
        const candidate = geometry.candidates.find((item: any) =>
            [item.aEndpointId, item.bEndpointId].includes(selection.id) && [item.aEndpointId, item.bEndpointId].includes(endpoint.id));
        const segment = { id: crypto.randomUUID(), aEndpointId: selection.id, bEndpointId: endpoint.id,
            a, b, routeType: 'curve', control: candidate?.curveDefaultControl || [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2],
            tangent: [b[0] - a[0], b[1] - a[1]], perimeterPoints: candidate?.perimeterRoute?.points || [],
            defaultControl: candidate?.curveDefaultControl, uiRoute: 'curve' };
        change({ ...settings, segments: [...settings.segments, segment] });
        setSelectedSegment(segment.id);
        setSelection(null);
    }

    function handleDrag(event: React.PointerEvent, type: string, id: string): void {
        event.stopPropagation();
        if (busy) return;
        history.current.push(structuredClone(settings));
        drag.current = { type, id };
        svgRef.current.setPointerCapture(event.pointerId);
    }

    function zoom(factor: number): void {
        setBox([box[0] + box[2] * (1 - factor) / 2, box[1] + box[3] * (1 - factor) / 2, box[2] * factor, box[3] * factor]);
    }

    async function apply(replace: boolean): Promise<void> {
        if (!result || applying || applied.includes(result.id)) return;
        setApplying(true);
        setError('');
        try {
            const current = await request(base);
            if (current.revision !== result.revision) throw new Error('Результат устарел. Выполните расчёт заново.');
            if (classes.some((item) => !mapping[item.key])) throw new Error('Сопоставьте все четыре класса с метками задания.');
            if (new Set(Object.values(mapping)).size !== 4) throw new Error('Для каждого класса нужна отдельная метка.');
            const existing = await job.annotations.get(frame, false, []);
            const previous = existing.filter((state: any) => {
                const attribute = state.label.attributes.find((attr: any) => attr.name === 'ore_run');
                return attribute && state.attributes[attribute.id]?.startsWith('ore:');
            });
            if (replace && job.labels.some((label: any) => Object.values(mapping).includes(label.id) && !label.attributes.some((attr: any) => attr.name === 'ore_run'))) {
                throw new Error('Для замены добавьте к выбранным меткам текстовый атрибут ore_run.');
            }
            if (replace && previous.length && !window.confirm('Заменить предыдущие маски доразметки? Ручные изменения этих масок будут удалены. Действие можно отменить в CVAT.')) return;
            const states = result.result.masks.map((mask: any) => {
                const label = job.labels.find((item: any) => item.id === Number(mapping[mask.className]));
                const attribute = label.attributes.find((attr: any) => attr.name === 'ore_run');
                return new core.classes.ObjectState({
                    objectType: ObjectType.SHAPE, shapeType: ShapeType.MASK, label, frame,
                    source: Source.AUTO, points: mask.points, occluded: false, zOrder: 0,
                    attributes: attribute ? { [attribute.id]: `ore:${result.id}` } : {},
                });
            });
            // Use CVAT's creation action so masks participate in its editor history and save flow.
            const ids = await dispatch(createAnnotationsAsync(states));
            if (ids.length !== states.length) throw new Error('CVAT не смог применить маски. Проверьте сообщение редактора.');
            if (replace) {
                for (const state of previous) await state.delete(frame, true);
                await dispatch(fetchAnnotationsAsync());
            }
            setApplied([...applied, result.id]);
            setNotice('Маски добавлены. Сохраните задание в CVAT.');
        } catch (exc) { setError(exc.message); }
        finally { setApplying(false); }
    }

    async function uploadSketch(event: React.ChangeEvent<HTMLInputElement>): Promise<void> {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        setBusy(true);
        try {
            const saved = await save();
            const form = new FormData();
            form.append('file', file);
            const data = await request(`${base}/sketch?revision=${saved.revision}`, { method: 'POST', body: form });
            setRecord({ ...record, ...data, hasSketch: true });
            setSettings(data.state);
            setGeometry(null);
            setResult(null);
            setNotice('Эскиз загружен.');
        } catch (exc) { setError(exc.message); }
        finally { setBusy(false); }
    }

    function exportSketch(): void {
        if (!geometry || geometryPending || busy) return;
        const canvas = document.createElement('canvas');
        canvas.width = record.width;
        canvas.height = record.height;
        const painter = canvas.getContext('2d');
        painter.fillStyle = 'white';
        painter.fillRect(0, 0, canvas.width, canvas.height);
        painter.strokeStyle = '#0037ff';
        painter.lineWidth = Math.max(3, record.width / 300);
        for (const line of geometry?.lines || []) for (const path of line.paths) painter.stroke(new Path2D(path));
        for (const segment of settings.segments) painter.stroke(new Path2D(segmentPath(segment)));
        canvas.toBlob((blob) => {
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = `sketch-${job.id}-${frame}.png`;
            anchor.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
    }

    const selected = settings?.segments.find((segment: any) => segment.id === selectedSegment);
    function setRoute(route: string): void {
        const next = structuredClone(settings);
        const segment = next.segments.find((item: any) => item.id === selectedSegment);
        segment.uiRoute = route;
        segment.tangent = [segment.b[0] - segment.a[0], segment.b[1] - segment.a[1]];
        if (route === 'straight') segment.control = [(segment.a[0] + segment.b[0]) / 2, (segment.a[1] + segment.b[1]) / 2];
        else if (route === 'curve') segment.control = segment.defaultControl || [(segment.a[0] + segment.b[0]) / 2, (segment.a[1] + segment.b[1]) / 2];
        else {
            const points = segment.perimeterPoints;
            const edge = points.find((p: Point) => p[0] === 0 || p[1] === 0 || p[0] >= record.width - 1 || p[1] >= record.height - 1);
            segment.control = edge || [0, record.height / 2];
            segment.tangent = [record.width * 4, record.height * 4];
        }
        change(next);
    }
    function segmentPath(segment: any): string {
        const normalized = geometry?.segments.find((item: any) => item.id === segment.id &&
            JSON.stringify(item.control) === JSON.stringify(segment.control) && JSON.stringify(item.tangent) === JSON.stringify(segment.tangent));
        if (normalized) return `M ${normalized.points.map((p: Point) => p.join(' ')).join(' L ')}`;
        if (segment.uiRoute === 'perimeter' && segment.perimeterPoints?.length) return `M ${segment.perimeterPoints.map((p: Point) => p.join(' ')).join(' L ')}`;
        return curve(segment);
    }
    const mainKeys = ['max_work_side', 'ore_boundary_shift', 'talc_dark_quantile', 'illumination_correction_strength', 'local_shadow_correction_strength'];
    function field(group: string, key: string): JSX.Element {
        const value = settings[group][key];
        return <label key={key} className='ore-field'><span>{names[key] || key}</span>
            <input aria-label={names[key] || key} type='number' min={record.bounds?.[key]?.[0]} max={record.bounds?.[key]?.[1]} step={Number.isInteger(value) && !key.includes('strength') ? '1' : '.01'}
                value={value} disabled={busy} onChange={(event) => change({ ...settings, [group]: { ...settings[group], [key]: Number(event.target.value) } })} />
        </label>;
    }

    return <div className='ore-backdrop' role='dialog' aria-modal='true' aria-label='Доразметка шлифа' onKeyDown={(event) => event.stopPropagation()}>
        <div className='ore-panel'>
            <header><strong>Доразметка шлифа</strong><span>Задание {job.id} · Кадр {frame}</span>
                <button title='Закрыть' aria-label='Закрыть' onClick={closeSaved}><CloseOutlined /></button></header>
            {error && <div className='ore-error' role='alert'>{error}</div>}
            {notice && <div className='ore-notice' role='status'>{notice}</div>}
            {!settings ? <div className='ore-loading'>{error ? 'Не удалось открыть кадр' : 'Загрузка кадра…'}</div> :
                <div className='ore-layout'>
                    <aside>
                        <label className='ore-field'><span>Алгоритм</span><select value={settings.algorithm} disabled={busy}
                            onChange={(e) => change({ ...settings, algorithm: e.target.value })}>
                            <option value='approach2'>Цветовая доразметка</option><option value='approach1'>Базовая кластеризация</option>
                        </select></label>
                        {Object.keys(settings[settings.algorithm]).filter((key) => mainKeys.includes(key)).map((key) => field(settings.algorithm, key))}
                        <details><summary>Расширенные настройки</summary>
                            {Object.keys(settings[settings.algorithm]).filter((key) => !mainKeys.includes(key)).map((key) => field(settings.algorithm, key))}</details>
                        <details><summary>Экспертный эскиз</summary>{Object.keys(settings.sketch).map((key) => field('sketch', key))}
                            <div className='ore-actions'><button disabled={busy} onClick={() => upload.current.click()}><UploadOutlined /> Импорт эскиза</button>
                                <button disabled={!geometry || geometryPending || busy} onClick={exportSketch}
                                    title={geometryPending ? 'Сначала обновите контуры' : 'Скачать эскиз PNG'}><DownloadOutlined /></button>
                                <button disabled={busy} title='Очистить эскиз' onClick={async () => {
                                    if (!window.confirm('Очистить экспертный эскиз этого кадра?')) return;
                                    try {
                                        const saved = await request(`${base}/sketch?revision=${record.revision}`, { method: 'DELETE' });
                                        setRecord({ ...record, ...saved, hasSketch: false });
                                        setSettings(saved.state); setGeometry(null); setResult(null); setSelectedSegment(null);
                                    } catch (exc) { setError(exc.message); }
                                }}><DeleteOutlined /></button></div>
                        </details>
                        <label className='ore-check'><input type='checkbox' checked={settings.corrected} disabled={busy || settings.algorithm !== 'approach2'}
                            onChange={(e) => change({ ...settings, corrected: e.target.checked })} /> Коррекция по эскизу</label>
                        <label className='ore-field'><span>Область коррекции</span><select disabled={busy} value={settings.regionMode}
                            onChange={(e) => change({ ...settings, regionMode: e.target.value })}>
                            <option value='inside'>Внутри контура</option><option value='outside'>Снаружи контура</option></select></label>
                        <details><summary>Настройки коррекции</summary>{Object.keys(settings.correction).map((key) => field('correction', key))}</details>
                        <div className='ore-actions'><button disabled={busy} onClick={() => calculate('geometry')}><LinkOutlined /> Обновить контуры</button></div>
                        <button className='ore-primary' disabled={busy} onClick={() => calculate('segmentation')}><PlayCircleOutlined /> Рассчитать маски</button>
                        {busy && <div className='ore-progress' role='status'>{work?.status === 'queued' ? `Очередь: ${work.position}` : 'Расчёт…'} {work?.elapsed || 0} с
                            <button title='Отменить расчёт' onClick={() => activeWork.current && request(`/jobs/${activeWork.current}`, { method: 'DELETE' }).catch((exc) => setError(exc.message))}><StopOutlined /></button></div>}
                        <details><summary>Метки CVAT</summary>{classes.map((item) => <label className='ore-field' key={item.key}><span>{item.title}</span>
                            <select value={mapping[item.key] || ''} onChange={(e) => setMapping({ ...mapping, [item.key]: Number(e.target.value) })}>
                                <option value=''>Выберите метку</option>{job.labels.map((label: any) => <option key={label.id} value={label.id}>{label.name}</option>)}</select></label>)}</details>
                        <div className='ore-actions'><button title='Экспорт настроек JSON' onClick={() => download(`settings-${job.id}-${frame}.json`, JSON.stringify(settings, null, 2))}><DownloadOutlined /></button>
                            <button title='Импорт настроек JSON' disabled={busy} onClick={() => importSettings.current.click()}><UploadOutlined /></button>
                            <button title='Сохранить настройки' disabled={busy} onClick={() => save().then(() => setNotice('Настройки сохранены.')).catch((exc) => setError(exc.message))}>Сохранить</button></div>
                    </aside>
                    <main>
                        <div className='ore-toolbar'>
                            <div className='ore-segmented'><button className={view === 'sketch' ? 'active' : ''} onClick={() => setView('sketch')}><EditOutlined /> Эскиз</button>
                                <button className={view === 'result' ? 'active' : ''} disabled={!result} onClick={() => setView('result')}><EyeOutlined /> Результат</button></div>
                            <div className='ore-tools'>
                                <button title='Перемещение' className={mode === 'pan' ? 'active' : ''} onClick={() => setMode('pan')}><DragOutlined /></button>
                                <button title='Рисовать контур' className={mode === 'draw' ? 'active' : ''} onClick={() => setMode('draw')}><EditOutlined /></button>
                                <button title={geometryPending ? 'Сначала обновите контуры' : 'Соединять концы'} disabled={geometryPending}
                                    className={mode === 'connect' ? 'active' : ''} onClick={() => setMode('connect')}><LinkOutlined /></button>
                                <button title='Отменить правку эскиза' disabled={busy || !history.current.length} onClick={() => {
                                    setSettings(history.current.pop()); setGeometry(null); setResult(null); setSelectedSegment(null);
                                }}><UndoOutlined /></button>
                                <button title='Удалить выбранное соединение' disabled={!selected || busy} onClick={() => {
                                    change({ ...settings, segments: settings.segments.filter((item: any) => item.id !== selected.id) }); setSelectedSegment(null);
                                }}><DeleteOutlined /></button>
                                <button title='Увеличить' onClick={() => zoom(.8)}><ZoomInOutlined /></button>
                                <button title='Уменьшить' onClick={() => zoom(1.25)}><ZoomOutOutlined /></button>
                                <button title='Вписать изображение' onClick={() => setBox([0, 0, record.width, record.height])}><AimOutlined /></button>
                            </div>
                            {view === 'sketch' && selected && <div className='ore-segmented'>
                                <button disabled={busy} onClick={() => setRoute('straight')}>Прямая</button>
                                <button disabled={busy} onClick={() => setRoute('curve')}>Кривая</button>
                                <button disabled={busy || !selected.perimeterPoints?.length} onClick={() => setRoute('perimeter')}>По краю</button>
                            </div>}
                            {view === 'result' && <label>Наложение <input aria-label='Прозрачность маски' type='range' min='0' max='1' step='.05' value={opacity} onChange={(e) => setOpacity(Number(e.target.value))} /></label>}
                            {view === 'result' && <label><input type='checkbox' checked={showSketch} disabled={busy}
                                onChange={(event) => toggleSketch(event.target.checked)} /> Показать эскиз</label>}
                        </div>
                        <div className='ore-canvas'>
                            <svg ref={svgRef} viewBox={box.join(' ')} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp}
                                onPointerCancel={pointerUp} onWheel={(event) => zoom(event.deltaY > 0 ? 1.1 : .9)}>
                                <image href={`${API}${base}/image`} width={record.width} height={record.height} />
                                {view === 'result' && result && <image href={`${API}/jobs/${result.id}/mask.png`} width={record.width} height={record.height} opacity={opacity} />}
                                {(view === 'sketch' || (view === 'result' && showSketch)) && <g fill='none'
                                    pointerEvents={view === 'result' ? 'none' : undefined} strokeWidth={Math.max(3, box[2] / 450)}>
                                    {view === 'sketch' && !geometry && record.hasSketch && <image href={`${API}${base}/sketch?revision=${record.revision}`}
                                        width={record.width} height={record.height} style={{ mixBlendMode: 'multiply' }} pointerEvents='none' />}
                                    {(view === 'sketch' && !geometryPending ? geometry?.regionChoices || [] : []).filter((choice: any) => choice.id === settings.regionMode).map((choice: any) =>
                                        <path key={choice.id} d={choice.path} fill='#00dd8025' fillRule='evenodd' stroke='none' pointerEvents='none' />)}
                                    {(geometry?.lines || []).map((line: any) => line.paths.map((path: string, index: number) =>
                                        <path key={`${line.id}-${index}`} d={path} stroke={line.closed ? '#ff4040' : '#246aff'} />))}
                                    {view === 'sketch' && pendingStrokes.map((stroke: Point[], index: number) => <polyline key={`stroke-${index}`} points={stroke.map((p) => p.join(',')).join(' ')} stroke='#246aff' />)}
                                    {settings.segments.map((segment: any) => <path key={segment.id} d={segmentPath(segment)} stroke={view === 'sketch' && segment.id === selectedSegment ? '#00dd80' : geometry?.segments.find((s: any) => s.id === segment.id)?.mainColor === 'red' ? '#ff4040' : '#246aff'}
                                        onPointerDown={(event) => { event.stopPropagation(); setSelectedSegment(segment.id); }} style={{ cursor: 'pointer' }} />)}
                                    {view === 'sketch' && <polyline points={draft.map((p) => p.join(',')).join(' ')} stroke='#246aff' />}
                                    {view === 'sketch' && mode === 'connect' && !geometryPending && (geometry?.endpoints || []).filter((endpoint: any) => !settings.segments.some((s: any) => s.aEndpointId === endpoint.id || s.bEndpointId === endpoint.id))
                                        .map((endpoint: any) => <circle key={endpoint.id} role='button' aria-label={`Конец контура ${endpoint.id}`} cx={endpoint.x} cy={endpoint.y} r={box[2] / 110}
                                            fill={selection?.id === endpoint.id ? '#00dd80' : '#fff'} stroke='#246aff' style={{ cursor: 'pointer' }}
                                            onPointerDown={(event) => { event.stopPropagation(); connectEndpoint(endpoint); }} />)}
                                    {view === 'sketch' && selected && <g stroke='#00dd80'>
                                        <line x1={selected.control[0]} y1={selected.control[1]} x2={selected.control[0] + selected.tangent[0]} y2={selected.control[1] + selected.tangent[1]} />
                                        <circle cx={selected.control[0]} cy={selected.control[1]} r={box[2] / 100} fill='#fff' style={{ cursor: 'move' }}
                                            onPointerDown={(event) => handleDrag(event, 'control', selected.id)} />
                                        <circle cx={selected.control[0] + selected.tangent[0]} cy={selected.control[1] + selected.tangent[1]} r={box[2] / 120} fill='#00dd80' style={{ cursor: 'move' }}
                                            onPointerDown={(event) => handleDrag(event, 'tangent', selected.id)} /></g>}
                                </g>}
                            </svg>
                        </div>
                        <footer><div className='ore-legend'>{classes.map((item) => <span key={item.key}><i style={{ background: item.color }} />{item.title}
                            {result ? ` ${result.result.stats[item.key]}%` : ''}</span>)}</div>
                            <div className='ore-actions'><button disabled={!result || busy || applying || applied.includes(result?.id)} onClick={() => apply(false)}>Добавить маски</button>
                                <button disabled={!result || busy || applying || applied.includes(result?.id)} onClick={() => apply(true)}>Заменить предыдущие</button></div></footer>
                    </main>
                </div>}
            <input ref={upload} type='file' accept='image/png,image/jpeg,image/tiff,.tif,.tiff' hidden onChange={uploadSketch} />
            <input ref={importSettings} type='file' accept='application/json,.json' hidden onChange={async (event) => {
                const file = event.target.files?.[0]; event.target.value = '';
                if (!file) return;
                try {
                    const state = JSON.parse(await file.text());
                    const saved = await request(base, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ revision: record.revision, state }) });
                    setRecord({ ...record, ...saved });
                    change(saved.state);
                    setGeometry(null);
                } catch (exc) { setError(exc.message || 'Некорректный JSON настроек.'); }
            }} />
        </div>
    </div>;
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;
function closePanel(): void {
    root?.unmount();
    root = null;
    host?.remove();
    host = null;
}
function register(): void {
    (window as any).cvatUI.registerComponent((context: any) => {
        const MenuItem = ({ key }: { key: string }): any => ({
            key: `ore-${key}`, label: 'Доразметка шлифа', onClick: () => {
                closePanel();
                host = document.createElement('div');
                document.body.appendChild(host);
                root = createRoot(host);
                root.render(<Panel context={context} close={closePanel} />);
            },
        });
        context.dispatch(context.actionCreators.addUIComponent('annotationPage.menuActions.items', MenuItem, { weight: 20 }));
        return { name: 'Ore thin sections', destructor: () => {
            closePanel();
            context.dispatch(context.actionCreators.removeUIComponent('annotationPage.menuActions.items', MenuItem));
        } };
    });
}
if ((window as any).cvatUI) register();
else window.addEventListener('plugins.ready', register, { once: true });
