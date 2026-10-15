// utils/mapCommands.js — 地图命令分发器（从 App.jsx 抽离）
// 执行由后端返回的 map_commands 数组

export function executeMapCommands(mapManager, commands) {
  if (!mapManager || !commands || commands.length === 0) {
    return;
  }

  commands.forEach(command => {
    if (!command || !command.type) {
      console.warn('Invalid or missing command type:', command);
      return;
    }

    try {
      switch (command.type) {
        case 'add_marker':
          const { lat, lng, title, popup } = command;
          if (typeof lat === 'undefined' || typeof lng === 'undefined') {
            console.warn('Missing coordinates for add_marker command:', command);
            break;
          }
          if (popup) {
            mapManager.addMarkerWithPopup(lat, lng, popup, { title });
          } else {
            mapManager.addMarker(lat, lng, { title });
          }
          break;

        case 'set_view':
          const { lat: viewLat, lng: viewLng, zoom } = command;
          if (typeof viewLat === 'undefined' || typeof viewLng === 'undefined') {
            console.warn('Missing coordinates for set_view command:', command);
            break;
          }
          mapManager.flyTo(viewLat, viewLng, zoom);
          break;

        case 'clear_markers':
          mapManager.clearMarkers();
          break;

        case 'switch_layer':
          if (command.layer) {
            mapManager.executeMapCommand(command);
          } else {
            console.warn('Missing layer for switch_layer command:', command);
          }
          break;

        case 'add_circle':
          const { lat: circleLat, lng: circleLng, radius, color } = command;
          if (typeof circleLat === 'undefined' || typeof circleLng === 'undefined' || typeof radius === 'undefined') {
            console.warn('Missing required data for add_circle command:', command);
            break;
          }
          mapManager.addCircle?.(circleLat, circleLng, radius, {
            color: color || 'blue',
            fillColor: color || 'blue',
            fillOpacity: 0.2
          });
          break;

        case 'add_circles':
          const { circles } = command;
          if (circles && Array.isArray(circles)) {
            mapManager.addCircles?.(circles);
          } else {
            console.warn('Missing or invalid circles data for add_circles command:', command);
          }
          break;

        case 'add_polygon':
          const { coordinates, color: polygonColor } = command;
          if (!coordinates || !Array.isArray(coordinates)) {
            console.warn('Missing or invalid coordinates for add_polygon command:', command);
            break;
          }
          const polygonCoords = coordinates.map(coord => [coord[1], coord[0]]);
          mapManager.addPolygon?.(polygonCoords, {
            color: polygonColor || 'red',
            fillColor: polygonColor || 'red',
            fillOpacity: 0.2
          });
          break;

        case 'add_polyline':
          const { coordinates: lineCoords, color: lineColor, weight } = command;
          if (!lineCoords || !Array.isArray(lineCoords)) {
            console.warn('Missing or invalid coordinates for add_polyline command:', command);
            break;
          }
          const leafletCoords = lineCoords.map(coord => [coord[1], coord[0]]);
          mapManager.addPolyline?.(leafletCoords, {
            color: lineColor || 'red',
            weight: weight || 3,
            opacity: 0.8
          });
          break;

        case 'fit_markers':
          mapManager.fitMarkers();
          break;

        // 新增：加载矢量图层
        case 'load_vector_layer':
          if (command.url && command.name) {
            mapManager.addVectorLayerFromAPI(command.url, command.name, {
              style: command.style,
              view: command.view,
            });
          } else {
            console.warn('Missing url or name for load_vector_layer command:', command);
          }
          break;

        default:
          console.warn('未知的地图命令类型:', command.type, command);
      }
    } catch (error) {
      console.error('执行地图命令失败:', error, command);
    }
  });
}