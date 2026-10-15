(function () {
  "use strict";

  var POLYGON_SPEED_MULTIPLIER = 2;

  function polygonTime(time) {
    var numericTime = Number(time);
    return (Number.isFinite(numericTime) ? numericTime : 0) * POLYGON_SPEED_MULTIPLIER;
  }

  window.UAPolygonMotion = {
    polygonSpeedMultiplier: POLYGON_SPEED_MULTIPLIER,
    polygonTime: polygonTime,
  };
}());
