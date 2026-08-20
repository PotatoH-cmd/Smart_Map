import { MapManager } from '../MapComponent.jsx';

describe('zoom-based visibility', () => {
  test('caiqu visible at zoom >= 11', () => {
    const mm = new MapManager('container', { zoom: 7 });
    mm.visibilityThresholds = { caiqu: 11, henanBaseMap: 12 };
    expect(mm.shouldShowLayer('caiqu', 10)).toBe(false);
    expect(mm.shouldShowLayer('caiqu', 11)).toBe(true);
    expect(mm.shouldShowLayer('caiqu', 15)).toBe(true);
  });

  test('henanBaseMap visible at zoom >= 12', () => {
    const mm = new MapManager('container', { zoom: 7 });
    mm.visibilityThresholds = { caiqu: 11, henanBaseMap: 12 };
    expect(mm.shouldShowLayer('henanBaseMap', 11)).toBe(false);
    expect(mm.shouldShowLayer('henanBaseMap', 12)).toBe(true);
  });
});
