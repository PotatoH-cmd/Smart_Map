/**
 * @license
 * Cesium - https://github.com/CesiumGS/cesium
 * Version 1.139.1
 *
 * Copyright 2011-2022 Cesium Contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Columbus View (Pat. Pend.)
 *
 * Portions licensed separately.
 * See https://github.com/CesiumGS/cesium/blob/main/LICENSE.md for full licensing details.
 */

import {
  EllipsoidGeometry_default
} from "./chunk-GLCPE2BJ.js";
import "./chunk-IJ3SUMK3.js";
import "./chunk-H3TVRJLY.js";
import "./chunk-CID5SY7L.js";
import "./chunk-CVAAIRNP.js";
import "./chunk-3UQ2YMPP.js";
import "./chunk-6U5QMZU3.js";
import "./chunk-JUKGMJX7.js";
import "./chunk-FCJQO4NC.js";
import "./chunk-PAFMAR7A.js";
import "./chunk-FXSRA5UG.js";
import "./chunk-YDR5LX5Z.js";
import "./chunk-PBECJOWF.js";
import "./chunk-UOQB7XQI.js";
import "./chunk-HNDE73JK.js";
import "./chunk-TXYQNXNW.js";
import "./chunk-N6HRZLS6.js";
import {
  defined_default
} from "./chunk-BTP3634E.js";

// packages/engine/Source/Workers/createEllipsoidGeometry.js
function createEllipsoidGeometry(ellipsoidGeometry, offset) {
  if (defined_default(offset)) {
    ellipsoidGeometry = EllipsoidGeometry_default.unpack(ellipsoidGeometry, offset);
  }
  return EllipsoidGeometry_default.createGeometry(ellipsoidGeometry);
}
var createEllipsoidGeometry_default = createEllipsoidGeometry;
export {
  createEllipsoidGeometry_default as default
};
