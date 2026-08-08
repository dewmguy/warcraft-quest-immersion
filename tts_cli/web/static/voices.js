const search = document.querySelector("#profile-search");
const gender = document.querySelector("#gender-filter");
const coverage = document.querySelector("#coverage-filter");
const cards = [...document.querySelectorAll(".profile-card")];
const count = document.querySelector("#profile-count");
const empty = document.querySelector("#profile-empty");

function filterProfiles() {
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  for (const card of cards) {
    const matchesSearch = !query || card.dataset.search.includes(query);
    const matchesGender = gender.value === "all" || card.dataset.gender === gender.value;
    const matchesCoverage = coverage.value === "all" || card.dataset.coverage === coverage.value;
    const show = matchesSearch && matchesGender && matchesCoverage;
    card.hidden = !show;
    if (show) visible += 1;
  }
  count.textContent = `Showing ${visible} of ${cards.length} profiles`;
  empty.hidden = visible !== 0;
}

search.addEventListener("input", filterProfiles);
gender.addEventListener("change", filterProfiles);
coverage.addEventListener("change", filterProfiles);
