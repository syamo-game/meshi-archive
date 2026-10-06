// @ts-check
(function () {
  'use strict';

  const areaSelect = document.getElementById('shop-area');
  const filters = document.getElementById('shop-area-filters');
  const prefectureSelect = document.getElementById('shop-area-prefecture');
  const queryInput = document.getElementById('shop-area-query');
  const resultStatus = document.getElementById('shop-area-results');
  if (!(areaSelect instanceof HTMLSelectElement) ||
      !(prefectureSelect instanceof HTMLSelectElement) ||
      !(queryInput instanceof HTMLInputElement) || !filters || !resultStatus) {
    throw new Error('Area picker controls are missing');
  }

  const groups = Array.from(areaSelect.querySelectorAll('optgroup')).map(function (group) {
    return {
      label: group.label,
      prefecture: group.label === '現在のエリア' ? '' : group.label.split(' / ')[0],
      options: Array.from(group.querySelectorAll('option')).map(function (option) {
        return { value: option.value, label: option.textContent || option.value };
      })
    };
  });
  const prefectures = Array.from(new Set(groups.map(function (group) {
    return group.prefecture;
  }).filter(Boolean)));
  for (const prefecture of prefectures) {
    prefectureSelect.add(new Option(prefecture, prefecture));
  }
  const initialGroup = groups.find(function (group) {
    return group.options.some(function (option) { return option.value === areaSelect.value; });
  });
  if (initialGroup) prefectureSelect.value = initialGroup.prefecture;

  function updateOptions() {
    const selectedValue = areaSelect.value;
    const selectedOption = areaSelect.selectedOptions[0];
    const selectedLabel = selectedOption ? selectedOption.textContent || selectedValue : selectedValue;
    const query = queryInput.value.normalize('NFKC').trim().toLocaleLowerCase('ja');
    const content = document.createDocumentFragment();
    content.append(new Option('未設定', ''));
    let foundSelected = !selectedValue;
    let count = 0;
    for (const group of groups) {
      if (prefectureSelect.value && group.prefecture !== prefectureSelect.value) continue;
      const options = group.options.filter(function (option) {
        const text = (group.label + ' ' + option.label + ' ' + option.value)
          .normalize('NFKC').toLocaleLowerCase('ja');
        return !query || text.includes(query);
      });
      if (!options.length) continue;
      const optgroup = document.createElement('optgroup');
      optgroup.label = group.label;
      for (const option of options) {
        optgroup.append(new Option(option.label, option.value));
        if (option.value === selectedValue) foundSelected = true;
        count += 1;
      }
      content.append(optgroup);
    }
    if (!foundSelected) {
      const currentGroup = document.createElement('optgroup');
      currentGroup.label = '現在選択中';
      currentGroup.append(new Option(selectedLabel, selectedValue));
      content.append(currentGroup);
    }
    areaSelect.replaceChildren(content);
    areaSelect.value = selectedValue;
    resultStatus.textContent = count
      ? count + '件の候補から選べます。'
      : '一致する候補がありません。都道府県や地名を変えてください。';
  }

  prefectureSelect.addEventListener('change', updateOptions);
  queryInput.addEventListener('input', updateOptions);
  filters.hidden = false;
  updateOptions();
}());
