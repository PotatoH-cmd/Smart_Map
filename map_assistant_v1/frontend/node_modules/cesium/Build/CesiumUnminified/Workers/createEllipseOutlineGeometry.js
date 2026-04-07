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
  EllipseOutlineGeometry_default
} from "./chunk-O67UP5YO.js";
import "./chunk-FKU77FNM.js";
import "./chunk-IJ3SUMK3.js";
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
import {
  Ellipsoid_default
} from "./chunk-UOQB7XQI.js";
import {
  Cartesian3_default
} from "./chunk-HNDE73JK.js";
import "./chunk-TXYQNXNW.js";
import "./chunk-N6HRZLS6.js";
import {
  defined_default
} from "./chunk-BTP3634E.js";

// packages/engine/Source/Workers/createEllipseOutlineGeometry.js
function createEllipseOutlineGeometry(ellipseGeometry, offset) {
  if (defined_default(offset)) {
    ellipseGeometry = EllipseOutlineGeometry_default.unpack(ellipseGeometry, offset);
  }
  ellipseGeometry._center = Cartesian3_default.clone(ellipseGeometry._center);
  ellipseGeometry._ellipsoid = Ellipsoid_default.clone(ellipseGeometry._ellipsoid);
  return EllipseOutlineGeometry_default.createGeometry(ellipseGeometry);
}
var createEllipseOutlineGeometry_default = createEllipseOutlineGeometry;
export {
  createEllipseOutlineGeometry_default as default
};
