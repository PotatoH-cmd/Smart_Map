// ============================================================
// Cesium 测量/绘制工具（自 CesiumComponent.jsx 拆分）
// ============================================================
import * as Cesium from 'cesium';
import { pickGroundCartesian } from './geoUtils';

// ==================== 测量 / 绘制 工具 ====================

const TOOL_GROUP = 'tool-draw-measure';

function clearToolEntities(viewer) {
  if (!viewer || viewer.isDestroyed()) return;
  const toRemove = viewer.entities.values.filter(e => e.toolGroup === TOOL_GROUP);
  toRemove.forEach(e => viewer.entities.remove(e));
}

function tagToolEntity(entity, sub = '') {
  entity.toolGroup = TOOL_GROUP;
  entity.toolSub = sub;
  return entity;
}

function formatLength(meters) {
  if (!Number.isFinite(meters)) return '-';
  if (meters >= 1000) return `${(meters / 1000).toFixed(2)} km`;
  return `${meters.toFixed(2)} m`;
}

function formatArea(sqm) {
  if (!Number.isFinite(sqm)) return '-';
  if (sqm >= 1_000_000) return `${(sqm / 1_000_000).toFixed(3)} km²`;
  if (sqm >= 10_000) return `${(sqm / 10_000).toFixed(3)} ha`;
  return `${sqm.toFixed(2)} m²`;
}

function totalDistance(positions) {
  let total = 0;
  for (let i = 1; i < positions.length; i++) {
    total += Cesium.Cartesian3.distance(positions[i - 1], positions[i]);
  }
  return total;
}

function polygonAreaFromPositions(positions) {
  if (!positions || positions.length < 3) return 0;
  const carto = positions.map(p => Cesium.Cartographic.fromCartesian(p));
  const R = 6378137;
  let total = 0;
  for (let i = 0; i < carto.length; i++) {
    const a = carto[i];
    const b = carto[(i + 1) % carto.length];
    total += (b.longitude - a.longitude) * (2 + Math.sin(a.latitude) + Math.sin(b.latitude));
  }
  return Math.abs(total * R * R / 2);
}

/**
 * 启动一个工具会话（测量或绘制）。返回销毁函数。
 * mode: 'distance' | 'area' | 'height' | 'point' | 'polyline' | 'polygon' | 'circle'
 */
function startToolSession(viewer, mode, onFinish) {
  if (!viewer || viewer.isDestroyed()) return () => {};
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
  const positions = [];
  let movePosition = null;
  const tempEntities = [];
  let labelEntity = null;

  const dynamicPositions = new Cesium.CallbackProperty(() => {
    const arr = positions.slice();
    if (movePosition) arr.push(movePosition);
    return arr;
  }, false);

  const dynamicHierarchy = new Cesium.CallbackProperty(() => {
    const arr = positions.slice();
    if (movePosition && arr.length >= 1) arr.push(movePosition);
    return new Cesium.PolygonHierarchy(arr.length >= 3 ? arr : []);
  }, false);

  // 预览图形
  if (mode === 'distance' || mode === 'polyline') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      polyline: {
        positions: dynamicPositions,
        width: 3,
        material: Cesium.Color.YELLOW,
        clampToGround: true,
      },
    }, mode)));
  } else if (mode === 'area' || mode === 'polygon') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      polyline: {
        positions: new Cesium.CallbackProperty(() => {
          const arr = positions.slice();
          if (movePosition) arr.push(movePosition);
          if (arr.length >= 2) arr.push(arr[0]);
          return arr;
        }, false),
        width: 2,
        material: Cesium.Color.YELLOW,
        clampToGround: true,
      },
      polygon: {
        hierarchy: dynamicHierarchy,
        material: Cesium.Color.YELLOW.withAlpha(0.3),
        classificationType: Cesium.ClassificationType.BOTH,
      },
    }, mode)));
  } else if (mode === 'circle') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      position: new Cesium.CallbackProperty(() => positions[0], false),
      ellipse: {
        semiMajorAxis: new Cesium.CallbackProperty(() => {
          if (positions.length === 0) return 0;
          const ref = movePosition || positions[1];
          if (!ref) return 0;
          return Math.max(Cesium.Cartesian3.distance(positions[0], ref), 1);
        }, false),
        semiMinorAxis: new Cesium.CallbackProperty(() => {
          if (positions.length === 0) return 0;
          const ref = movePosition || positions[1];
          if (!ref) return 0;
          return Math.max(Cesium.Cartesian3.distance(positions[0], ref), 1);
        }, false),
        material: Cesium.Color.YELLOW.withAlpha(0.3),
        outline: true,
        outlineColor: Cesium.Color.YELLOW,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    }, mode)));
  }

  function addVertex(pos) {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      position: pos,
      point: {
        pixelSize: 8,
        color: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.YELLOW,
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    }, mode)));
  }

  function updateLabel() {
    let text = '';
    if (mode === 'distance' && positions.length >= 1) {
      const arr = movePosition ? positions.concat([movePosition]) : positions;
      text = `距离: ${formatLength(totalDistance(arr))}`;
    } else if (mode === 'area' && positions.length >= 2) {
      const arr = movePosition ? positions.concat([movePosition]) : positions;
      text = `面积: ${formatArea(polygonAreaFromPositions(arr))}\n周长: ${formatLength(totalDistance(arr) + (arr.length >= 3 ? Cesium.Cartesian3.distance(arr[arr.length - 1], arr[0]) : 0))}`;
    } else if (mode === 'circle' && positions.length >= 1) {
      const ref = movePosition || positions[1];
      if (ref) {
        const r = Cesium.Cartesian3.distance(positions[0], ref);
        text = `半径: ${formatLength(r)}\n面积: ${formatArea(Math.PI * r * r)}`;
      }
    }
    if (!text) {
      if (labelEntity) { viewer.entities.remove(labelEntity); labelEntity = null; }
      return;
    }
    const anchor = movePosition || positions[positions.length - 1];
    if (!anchor) return;
    if (!labelEntity) {
      labelEntity = viewer.entities.add(tagToolEntity({
        position: new Cesium.CallbackProperty(() => movePosition || positions[positions.length - 1], false),
        label: {
          text,
          font: '13px sans-serif',
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString('#1f2937').withAlpha(0.85),
          pixelOffset: new Cesium.Cartesian2(12, -12),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'label'));
    } else {
      labelEntity.label.text = text;
    }
  }

  function finish(commit = true) {
    handler.destroy();
    if (!commit) {
      tempEntities.forEach(e => viewer.entities.remove(e));
      if (labelEntity) viewer.entities.remove(labelEntity);
    } else if (mode === 'height' && positions.length === 1) {
      // 高度测量：在终点显示
    }
    movePosition = null;
    if (typeof onFinish === 'function') onFinish({ mode, positions: positions.slice() });
  }

  handler.setInputAction(event => {
    const pos = pickGroundCartesian(viewer, event.position);
    if (!pos) return;
    if (mode === 'point') {
      const carto = Cesium.Cartographic.fromCartesian(pos);
      const lng = Cesium.Math.toDegrees(carto.longitude);
      const lat = Cesium.Math.toDegrees(carto.latitude);
      viewer.entities.add(tagToolEntity({
        position: pos,
        point: {
          pixelSize: 10, color: Cesium.Color.CYAN,
          outlineColor: Cesium.Color.WHITE, outlineWidth: 2,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
        label: {
          text: `${lng.toFixed(5)}, ${lat.toFixed(5)}`,
          font: '12px sans-serif', fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -16),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'point'));
      finish(true);
      return;
    }
    if (mode === 'height') {
      const carto = Cesium.Cartographic.fromCartesian(pos);
      const groundHeight = viewer.scene.globe.getHeight(carto) || carto.height || 0;
      const top = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, groundHeight + 1);
      viewer.entities.add(tagToolEntity({
        position: pos,
        point: { pixelSize: 8, color: Cesium.Color.CYAN, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
        label: {
          text: `高程: ${groundHeight.toFixed(2)} m`,
          font: '13px sans-serif', fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString('#1f2937').withAlpha(0.85),
          pixelOffset: new Cesium.Cartesian2(0, -16),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'height'));
      void top;
      finish(true);
      return;
    }
    positions.push(pos);
    addVertex(pos);
    if (mode === 'circle' && positions.length >= 2) {
      updateLabel();
      finish(true);
      return;
    }
    updateLabel();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  handler.setInputAction(event => {
    movePosition = pickGroundCartesian(viewer, event.endPosition);
    updateLabel();
    viewer.scene.requestRender();
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  handler.setInputAction(() => {
    if (mode === 'distance' || mode === 'polyline') {
      if (positions.length >= 2) { movePosition = null; updateLabel(); finish(true); }
      else finish(false);
    } else if (mode === 'area' || mode === 'polygon') {
      if (positions.length >= 3) { movePosition = null; updateLabel(); finish(true); }
      else finish(false);
    } else {
      finish(false);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  handler.setInputAction(() => {
    finish(false);
  }, Cesium.ScreenSpaceEventType.RIGHT_CLICK);

  return () => { if (!handler.isDestroyed()) finish(false); };
}
export { TOOL_GROUP, pickGroundCartesian, clearToolEntities, tagToolEntity, startToolSession };
