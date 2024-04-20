import { ListingsRetriever } from './util/listingsRetriever.js';

function formDataRetriever() {
    return {};
}

document.addEventListener('DOMContentLoaded', function() {
    const elems = document.querySelectorAll('select');
    M.FormSelect.init(elems, {}); // Assuming you're using Materialize for form elements

    const listingsRetriever = new ListingsRetriever(formDataRetriever, '/saved');
    listingsRetriever.fetchListings();
});
