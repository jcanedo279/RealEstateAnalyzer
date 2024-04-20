document.addEventListener('DOMContentLoaded', function() {
    // Initialize the left side sidenav
    var leftNavElems = document.querySelectorAll('.sidenav-left');
    M.Sidenav.init(leftNavElems, { edge: 'left' });

    // Initialize the right side sidenav
    var rightNavElems = document.querySelectorAll('.sidenav-right');
    M.Sidenav.init(rightNavElems, { edge: 'right' });
})
