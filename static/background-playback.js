/* Share the OS media session between the two players; the last played one owns it. */
(function () {
    'use strict';
    var owner = null;
    var players = new Set();
    var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

    function playbackSession() {
        try {
            if (navigator.audioSession) navigator.audioSession.type = 'playback';
        } catch (error) { /* Experimental API: native playback remains available. */ }
    }

    function attach(player, getMetadata) {
        players.add(player);
        var session = navigator.mediaSession;
        function metadata() {
            if (!session || owner !== player || !window.MediaMetadata) return;
            var item = getMetadata();
            try {
                session.metadata = new MediaMetadata({
                    title: item.title || '', artist: item.artist || '', album: item.album || '',
                    artwork: player.poster() ? [{ src: player.poster() }] : []
                });
            } catch (error) { /* Ignore unsupported metadata/artwork. */ }
        }
        function position() {
            if (!session || owner !== player || !session.setPositionState) return;
            var duration = player.duration();
            var time = player.currentTime();
            try {
                if (!Number.isFinite(duration) || duration <= 0 || !Number.isFinite(time)) {
                    session.setPositionState();
                    return;
                }
                session.setPositionState({ duration: duration,
                    playbackRate: player.playbackRate(), position: Math.max(0, Math.min(time, duration)) });
            } catch (error) { /* Some browsers expose only part of Media Session. */ }
        }
        function action(name, handler) {
            try { session.setActionHandler(name, handler); } catch (error) { /* Optional action. */ }
        }
        function seek(time) {
            var duration = player.duration();
            if (Number.isFinite(time) && Number.isFinite(duration) && duration > 0) {
                player.currentTime(Math.max(0, Math.min(time, duration)));
                position();
            }
        }
        player.on('play', function () {
            owner = player;
            players.forEach(function (other) { if (other !== player) other.pause(); });
            playbackSession();
            if (!session) return;
            metadata();
            session.playbackState = 'playing';
            action('play', function () {
                playbackSession();
                var result = player.play();
                if (result && result.catch) result.catch(function () { session.playbackState = 'paused'; });
            });
            action('pause', function () { player.pause(); });
            action('seekbackward', function (event) { seek(player.currentTime() - (event.seekOffset || 10)); });
            action('seekforward', function (event) { seek(player.currentTime() + (event.seekOffset || 10)); });
            action('seekto', function (event) { seek(event.seekTime); });
            position();
        });
        player.on('pause', function () {
            if (session && owner === player) session.playbackState = 'paused';
        });
        player.on('ended', function () {
            if (session && owner === player) session.playbackState = 'none';
        });
        ['loadedmetadata', 'posterchange'].forEach(function (event) { player.on(event, metadata); });
        ['loadedmetadata', 'durationchange', 'timeupdate', 'seeked', 'ratechange', 'emptied'].forEach(function (event) {
            player.on(event, position);
        });
        player.on('dispose', function () {
            players.delete(player);
            if (owner !== player) return;
            owner = null;
            if (!session) return;
            ['play', 'pause', 'seekbackward', 'seekforward', 'seekto'].forEach(function (name) { action(name, null); });
            session.metadata = null;
            session.playbackState = 'none';
        });
    }
    window.BackgroundPlayback = { attach: attach, isIOS: isIOS };
})();
