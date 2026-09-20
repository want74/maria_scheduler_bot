import requests
import json

# 1. Загружаем список всех групп
url = "https://ruz.guz.ru/api/dictionary/groups"
response = requests.get(url)
groups = response.json()

# 2. Задаём критерии поиска
target_year = 2026
target_kind_edu = 0 # например, магистратура
target_faculty_oid = 1  # Факультет землеустройства и экономики землепользования
target_course = 4
target_speciality_part = "Землеустройство и кадастры"  # часть строки speciality
target_specialization = "Землеустройство"  # groupSpecialization_name

# 3. Фильтруем
found_groups = []
for g in groups:
    if (g.get("YearOfEducation") == target_year and
        g.get("kindEducation") == target_kind_edu and
        g.get("facultyOid") == target_faculty_oid and
        g.get("course") == target_course and
        target_speciality_part in g.get("speciality", "") and
        g.get("groupSpecialization_name") == target_specialization):
        found_groups.append(g)

# 4. Выводим результаты
for g in found_groups:
    print(f"groupOid: {g['groupOid']}, groupGUID: {g['groupGUID']}, name: {g['name']}")