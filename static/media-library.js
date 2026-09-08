/* The two players retain separate progress, subtitle/lyrics and summary state. */
(function () {
    const tabs = Array.from(document.querySelectorAll('[role="tab"][data-media]'));
    function selectMedia(media) {
        tabs.forEach(function (tab) {
            const selected = tab.dataset.media === media;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
            document.getElementById(tab.dataset.media + '-list').hidden = !selected;
            if (!selected) videojs.getPlayer(tab.dataset.media + '-player').pause();
        });
        const url = new URL(window.location.href);
        url.pathname = document.querySelector('.media-tabs').dataset.playerUrl;
        url.searchParams.set('tab', media);
        const active = document.querySelector('#' + media + '-list .playlist-item.active');
        if (active) url.searchParams.set('file', active.dataset.filename);
        else url.searchParams.delete('file');
        window.history.replaceState({}, '', url);
        window.dispatchEvent(new Event('resize'));
    }
    tabs.forEach(function (tab, index) {
        tab.addEventListener('click', function () { selectMedia(tab.dataset.media); });
        tab.addEventListener('keydown', function (event) {
            let next;
            if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') next = tabs[1 - index];
            if (event.key === 'Home') next = tabs[0];
            if (event.key === 'End') next = tabs[tabs.length - 1];
            if (!next) return;
            event.preventDefault();
            next.focus();
            selectMedia(next.dataset.media);
        });
    });
})();
