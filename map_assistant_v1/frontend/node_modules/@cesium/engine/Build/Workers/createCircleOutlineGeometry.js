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
  Cartesian3_default,
  Frozen_default
} from "./chunk-HNDE73JK.js";
import "./chunk-TXYQNXNW.js";
import {
  Check_default
} from "./chunk-N6HRZLS6.js";
import {
  defined_default
} from "./chunk-BTP3634E.js";

// packages/engine/Source/Core/CircleOutlineGeometry.js
function CircleOutlineGeometry(options) {
  options = options ?? Frozen_default.EMPTY_OBJECT;
  const radius = options.radius;
  Check_default.typeOf.number("radius", radius);
  const ellipseGeometryOptions = {
    center: options.center,
    semiMajorAxis: radius,
    semiMinorAxis: radius,
    ellipsoid: options.ellipsoid,
    height: options.height,
    extrudedHeight: options.extrudedHeight,
    granularity: options.granularity,
    numberOfVerticalLines: options.numberOfVerticalLines
  };
  this._ellipseGeometry = new EllipseOutlineGeometry_default(ellipseGeometryOptions);
  this._workerName = "createCircleOutlineGeometry";
}
CircleOutlineGeometry.packedLength = EllipseOutlineGeometry_default.packedLength;
CircleOutlineGeometry.pack = function(value, array, startingIndex) {
  Check_default.typeOf.object("value", value);
  return EllipseOutlineGeometry_default.pack(
    value._ellipseGeometry,
    array,
    startingIndex
  );
};
var scratchEllipseGeometry = new EllipseOutlineGeometry_default({
  center: new Cartesian3_default(),
  semiMajorAxis: 1,
  semiMinorAxis: 1
});
var scratchOptions = {
  center: new Cartesian3_default(),
  radius: void 0,
  ellipsoid: Ellipsoid_default.clone(Ellipsoid_default.UNIT_SPHERE),
  height: void 0,
  extrudedHeight: void 0,
  granularity: void 0,
  numberOfVerticalLines: void 0,
  semiMajorAxis: void 0,
  semiMinorAxis: void 0
};
CircleOutlineGeometry.unpack = function(array, startingIndex, result) {
  const ellipseGeometry = EllipseOutlineGeometry_default.unpack(
    array,
    startingIndex,
    scratchEllipseGeometry
  );
  scratchOptions.center = Cartesian3_default.clone(
    ellipseGeometry._center,
    scratchOptions.center
  );
  scratchOptions.ellipsoid = Ellipsoid_default.clone(
    ellipseGeometry._ellipsoid,
    scratchOptions.ellipsoid
  );
  scratchOptions.height = ellipseGeometry._height;
  scratchOptions.extrudedHeight = ellipseGeometry._extrudedHeight;
  scratchOptions.granularity = ellipseGeometry._granularity;
  scratchOptions.numberOfVerticalLines = ellipseGeometry._numberOfVerticalLines;
  if (!defined_default(result)) {
    scratchOptions.radius = ellipseGeometry._semiMajorAxis;
    return new CircleOutlineGeometry(scratchOptions);
  }
  scratchOptions.semiMajorAxis = ellipseGeometry._semiMajorAxis;
  scratchOptions.semiMinorAxis = ellipseGeometry._semiMinorAxis;
  result._ellipseGeometry = new EllipseOutlineGeometry_default(scratchOptions);
  return result;
};
CircleOutlineGeometry.createGeometry = function(circleGeometry) {
  return EllipseOutlineGeometry_default.createGeometry(circleGeometry._ellipseGeometry);
};
var CircleOutlineGeometry_default = CircleOutlineGeometry;

// packages/engine/Source/Workers/createCircleOutlineGeometry.js
function createCircleOutlineGeometry(circleGeometry, offset) {
  if (defined_default(offset)) {
    circleGeometry = CircleOutlineGeometry_default.unpack(circleGeometry, offset);
  }
  circleGeometry._ellipseGeometry._center = Cartesian3_default.clone(
    circleGeometry._ellipseGeometry._center
  );
  circleGeometry._ellipseGeometry._ellipsoid = Ellipsoid_default.clone(
    circleGeometry._ellipseGeometry._ellipsoid
  );
  return CircleOutlineGeometry_default.createGeometry(circleGeometry);
}
var createCircleOutlineGeometry_default = createCircleOutlineGeometry;
export {
  createCircleOutlineGeometry_default as default
};
