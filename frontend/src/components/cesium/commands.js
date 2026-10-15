// ============================================================
// Cesium 命令执行器（自 CesiumComponent.jsx 拆分）
// ============================================================
import * as Cesium from 'cesium';
import { loadBasemap, OBLIQUE_TILESETS } from './presets';
import {
  parseCesiumColor, createPinImage, isDepthGeoJsonCommand,
  removeDepthColumnGroup, addDepthColumns, clearDepthColumnGroups
} from './geoUtils';

// ==================== 命令执行器 ====================

// 模块级变量，供 executeCesiumCommand 访问本地数据集
let _moduleLocalDatasets = [];

function executeCesiumCommand(viewer, command) {
  if (!viewer || !command || !command.type) return;
  const { type } = command;
  try {
    switch (type) {
      case 'flyTo': {
        const { lat, lng, height = 100000, duration = 3 } = command;
        viewer.camera.flyTo({
          destination: Cesium.Cartesian3.fromDegrees(lng, lat, height),
          duration,
          easingFunction: Cesium.EasingFunction.QUADRATIC_IN_OUT,
          orientation: {
            heading: Cesium.Math.toRadians(0),
            pitch: Cesium.Math.toRadians(-45),
            roll: 0,
          },
        });
        break;
      }
      case 'setView': {
        const { lat, lng, height = 50000 } = command;
        viewer.camera.setView({
          destination: Cesium.Cartesian3.fromDegrees(lng, lat, height),
          orientation: {
            heading: Cesium.Math.toRadians(0),
            pitch: Cesium.Math.toRadians(-45),
            roll: 0,
          },
        });
        break;
      }
      case 'addMarker': {
        const { lat, lng, title = '标注点', popup = '', color = '#ff4444' } = command;
        const cesiumColor = parseCesiumColor(color);
        const entity = viewer.entities.add({
          name: title,
          position: Cesium.Cartesian3.fromDegrees(lng, lat),
          billboard: {
            image: createPinImage(cesiumColor),
            verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            scale: 1.0,
          },
          description: popup || title,
        });
        viewer.flyTo(entity, { duration: 2 });
        break;
      }
      case 'addGeoJsonLayer': {
        const { url, name = 'GeoJSON图层', color = '#2773d7' } = command;
        let fullUrl = url;
        if (url && url.startsWith('/')) {
          fullUrl = url;  // 使用相对路径，由 CRA proxy 或 Nginx 反代转发到后端
        }
        const existing = viewer.dataSources.getByName(name);
        existing.forEach(ds => viewer.dataSources.remove(ds));
        removeDepthColumnGroup(viewer, name);
        if (isDepthGeoJsonCommand({ ...command, url: fullUrl, name })) {
          addDepthColumns(viewer, {
            ...command,
            type: 'addDepthColumns',
            url: fullUrl,
            name,
            threshold: command.threshold ?? 2,
            heightScale: command.heightScale ?? 45,
          });
          break;
        }
        Cesium.GeoJsonDataSource.load(fullUrl, {
          stroke: parseCesiumColor(color),
          fill: parseCesiumColor(color, 0.4),
          strokeWidth: 2,
          clampToGround: true,
        }).then(ds => {
          ds.name = name;
          viewer.dataSources.add(ds);
          viewer.flyTo(ds, { duration: 2 }).catch(() => {});
        }).catch(err => {
          console.error('[CesiumComponent] GeoJSON加载失败:', err, url);
        });
        break;
      }
      case 'addDepthColumns': {
        addDepthColumns(viewer, command);
        break;
      }
      case 'removeLayer': {
        viewer.dataSources.getByName(command.name).forEach(ds => viewer.dataSources.remove(ds));
        removeDepthColumnGroup(viewer, command.name);
        break;
      }
      case 'clearAll': {
        viewer.entities.removeAll();
        viewer.dataSources.removeAll();
        clearDepthColumnGroups(viewer);
        break;
      }
      case 'setBasemap': {
        loadBasemap(viewer, command.basemap);
        break;
      }
      case 'screenshot': {
        viewer.render();
        const imageData = viewer.canvas.toDataURL('image/png');
        window.dispatchEvent(new CustomEvent('cesium_screenshot', { detail: { imageData } }));
        const link = document.createElement('a');
        link.download = 'cesium_' + Date.now() + '.png';
        link.href = imageData;
        link.click();
        break;
      }
      case 'addPolygon': {
        const { coordinates, color = '#2773d7', name: polyName = '多边形' } = command;
        if (!coordinates || coordinates.length < 3) break;
        viewer.entities.add({
          name: polyName,
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(
              coordinates.map(([lng, lat]) => Cesium.Cartesian3.fromDegrees(lng, lat))
            ),
            material: parseCesiumColor(color, 0.5),
            outline: true,
            outlineColor: parseCesiumColor(color),
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          },
        });
        break;
      }
      case 'addPolyline': {
        const { coordinates, color = '#ff4444', name: lineName = '折线' } = command;
        if (!coordinates || coordinates.length < 2) break;
        viewer.entities.add({
          name: lineName,
          polyline: {
            positions: coordinates.map(([lng, lat]) => Cesium.Cartesian3.fromDegrees(lng, lat)),
            width: 3,
            material: parseCesiumColor(color),
            clampToGround: true,
          },
        });
        break;
      }
      case 'load3dTiles': {
        const preset = command.preset ? OBLIQUE_TILESETS[command.preset] : null;
        // 也尝试从 localDatasets 中查找 URL（当 preset 不存在但有 tileset key 时）
        const localDs = (!preset && command.key)
          ? (_moduleLocalDatasets || []).find(ds => ds.key === command.key)
          : null;
        const merged = preset
          ? { ...preset, ...command, url: command.url || preset.url, name: command.name || preset.name }
          : localDs
            ? { ...command, url: command.url || `/api/3dtiles/${encodeURIComponent(localDs.key)}/tileset.json`, name: command.name || localDs.label || localDs.key, autoGroundClamp: command.autoGroundClamp ?? (localDs.auto_ground_clamp !== false), altOffset: command.altOffset ?? (localDs.alt_offset || 0) }
            : command;
        const { url: tilesUrl, name = '3D Tiles' } = merged;
        if (!tilesUrl) break;
        if (!viewer._mapAssistantTilesets) viewer._mapAssistantTilesets = {};
        if (viewer._mapAssistantTilesets[name]) {
          try { viewer.scene.primitives.remove(viewer._mapAssistantTilesets[name]); } catch (_) {}
          delete viewer._mapAssistantTilesets[name];
        }
        window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'loading' } }));
        const tilesetOptions = {
          maximumScreenSpaceError: merged.maximumScreenSpaceError ?? 8,
          dynamicScreenSpaceError: merged.dynamicScreenSpaceError ?? true,
          dynamicScreenSpaceErrorDensity: merged.dynamicScreenSpaceErrorDensity ?? 0.00278,
          dynamicScreenSpaceErrorFactor: merged.dynamicScreenSpaceErrorFactor ?? 2,
          foveatedScreenSpaceError: merged.foveatedScreenSpaceError ?? true,
          foveatedConeSize: merged.foveatedConeSize ?? 0.5,
          foveatedMinimumScreenSpaceErrorRelaxation: merged.foveatedMinimumScreenSpaceErrorRelaxation ?? 1.2,
          foveatedTimeDelay: merged.foveatedTimeDelay ?? 0.2,
          skipLevelOfDetail: merged.skipLevelOfDetail ?? false,
          cullWithChildrenBounds: merged.cullWithChildrenBounds ?? true,
          maximumCacheOverflowBytes: merged.maximumCacheOverflowBytes ?? 512 * 1024 * 1024,
        };
        Cesium.Cesium3DTileset.fromUrl(tilesUrl, tilesetOptions).then(tileset => {
          tileset.name = name;
          // (altitude correction is applied below after primitive is ready)
          // ---- 高程修正：将模型贴合到平坦地形(0m)上 ----
          const _applyGroundClamp = () => {
            const center = tileset.boundingSphere.center;
            const carto = Cesium.Cartographic.fromCartesian(center);
            const centerAlt = carto.height;
            if (merged.autoGroundClamp && centerAlt > 50) {
              // 用包围球半径的15%估算垂直半高，将模型底部贴地
              const vertHalf = tileset.boundingSphere.radius * 0.15;
              const bottomAlt = centerAlt - vertHalf;
              const delta = -bottomAlt;
              const from = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0);
              const to = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, delta);
              const translation = Cesium.Cartesian3.subtract(to, from, new Cesium.Cartesian3());
              tileset.modelMatrix = Cesium.Matrix4.fromTranslation(translation);
            } else if (Number.isFinite(Number(merged.altOffset)) && Number(merged.altOffset) !== 0) {
              const surface = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0.0);
              const offset = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, Number(merged.altOffset));
              const translation = Cesium.Cartesian3.subtract(offset, surface, new Cesium.Cartesian3());
              tileset.modelMatrix = Cesium.Matrix4.fromTranslation(translation);
            }
          };
          _applyGroundClamp();
          viewer.scene.primitives.add(tileset);
          viewer._mapAssistantTilesets[name] = tileset;
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'ready' } }));
          if (merged.flyTo === false) {
            viewer.scene.requestRender();
          } else if (merged.center) {
            viewer.camera.flyTo({
              destination: Cesium.Cartesian3.fromDegrees(merged.center.lng, merged.center.lat, merged.center.height || 900),
              orientation: {
                heading: Cesium.Math.toRadians(merged.center.heading || 0),
                pitch: Cesium.Math.toRadians(merged.center.pitch || -37),
                roll: 0,
              },
              duration: merged.duration ?? 2,
            });
          } else {
            // 自动贴合 tileset 包围球，正上方俯视
            const bs = tileset.boundingSphere;
            const range = Math.max(bs.radius * 2.5, 300);
            viewer.camera.flyToBoundingSphere(bs, {
              offset: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_TWO, range),
              duration: 2,
            });
          }
        }).catch(err => {
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'error' } }));
          console.error('[CesiumComponent] 3DTiles加载失败:', err);
        });
        break;
      }
      case 'loadTerrain': {
        const { url: terrainUrl } = command;
        if (!terrainUrl) break;
        Cesium.CesiumTerrainProvider.fromUrl(terrainUrl).then(tp => {
          if (!viewer.isDestroyed()) viewer.terrainProvider = tp;
        }).catch(err => console.error('[CesiumComponent] 地形加载失败:', err));
        break;
      }
      default:
        console.warn('[CesiumComponent] 未知命令:', type);
    }
  } catch (err) {
    console.error('[CesiumComponent] 执行命令出错:', type, err);
  }
}

export { executeCesiumCommand };
export function setModuleLocalDatasets(datasets) { _moduleLocalDatasets = datasets; }
