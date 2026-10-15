// The lab's clock: slow motion and freeze for the product's effects.
//
// Loaded before any product script. It replaces the page's
// performance.now, requestAnimationFrame and setTimeout with versions that
// run on a lab clock, so effects that time themselves with them (the popup
// glass, the intro word, the background) slow down or stop together. CSS
// transitions and animations follow through their playback rate. Only this
// lab page does this; nothing in the product loads it.
(function () {
  "use strict";

  var realNow = performance.now.bind(performance);
  var realFrame = window.requestAnimationFrame.bind(window);
  var realClear = window.clearTimeout.bind(window);
  var speed = 1;
  var labBase = realNow();
  var realBase = labBase;
  var timers = new Map();
  var nextTimer = 1;

  function labNow() {
    return labBase + (realNow() - realBase) * speed;
  }

  performance.now = labNow;
  window.requestAnimationFrame = function (callback) {
    return realFrame(function () { callback(labNow()); });
  };
  // Timers wait in lab time, so an effect's own timers (such as the popup
  // glass's safety timer) keep pace with its frames.
  window.setTimeout = function (callback, delay) {
    var args = Array.prototype.slice.call(arguments, 2);
    var id = nextTimer++;
    timers.set(id, { callback: callback, at: labNow() + (Number(delay) || 0), args: args });
    return id;
  };
  window.clearTimeout = function (id) {
    if (!timers.delete(id)) realClear(id);
  };
  window.setInterval(function () {
    var now = labNow();
    timers.forEach(function (timer, id) {
      if (timer.at > now) return;
      timers.delete(id);
      if (typeof timer.callback === "function") timer.callback.apply(window, timer.args);
    });
  }, 8);

  // CSS transitions and animations (the popup's text fading in, the
  // overlay's fade) take the same speed.
  function paceCss() {
    if (document.getAnimations) {
      document.getAnimations().forEach(function (animation) {
        if (animation.playbackRate !== speed) animation.playbackRate = speed;
      });
    }
    realFrame(paceCss);
  }
  realFrame(paceCss);

  window.UALabClock = {
    speed: function () { return speed; },
    // 1 is normal speed, 0.25 a quarter, 0 frozen.
    setSpeed: function (next) {
      labBase = labNow();
      realBase = realNow();
      speed = Math.max(0, Number(next) || 0);
    },
  };
}());
