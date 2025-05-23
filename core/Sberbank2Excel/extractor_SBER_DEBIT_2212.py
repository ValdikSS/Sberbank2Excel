from . import exceptions
import re
from datetime import datetime
import sys
from typing import Any

from .utils import get_float_from_money
from .utils import split_Sberbank_line

from .extractor import Extractor
from . import extractors_generic

class SBER_DEBIT_2212(Extractor):

    def check_specific_signatures(self):

        test1 = re.search(r'сбербанк', self.bank_text, re.IGNORECASE)
        # print(f"{test1=}")

        test2 = re.search(r'Выписка по счёту дебетовой карты', self.bank_text, re.IGNORECASE)
        # print(f"{test2=}")

        test_ostatok_po_schetu = re.search(r'ОСТАТОК ПО СЧЁТУ', self.bank_text, re.IGNORECASE)

        if (not test1  or not test2) or test_ostatok_po_schetu:
            raise exceptions.InputFileStructureError("Не найдены паттерны, соответствующие выписке")

    def get_period_balance(self)->float:
        """
        функция ищет в тексте значения "ВСЕГО СПИСАНИЙ" и "ВСЕГО ПОПОЛНЕНИЙ" и возвращает разницу
        используется для контрольной проверки вычислений

        Пример текста
        ----------------------------------------------------------
        ОСТАТОК НА 30.06.2021     ОСТАТОК НА 06.07.2021     ВСЕГО СПИСАНИЙ     ВСЕГО ПОПОЛНЕНИЙ
        28 542,83->12 064,34->248 822,49->232 344,00
        ----------------------------------------------------------

        :param PDF_text:
        :return:
        """

        res = re.search(r'ОСТАТОК НА.*?ОСТАТОК НА.*?ВСЕГО СПИСАНИЙ.*?ВСЕГО ПОПОЛНЕНИЙ.*?\n(.*?)\n', self.bank_text, re.MULTILINE)
        if not res:
            raise exceptions.InputFileStructureError(
                'Не найдена структура с остатками и пополнениями')

        line_parts = res.group(1).split('\t')

        summa_spisaniy = line_parts[2]
        summa_popolneniy = line_parts[3]

        # print('summa_spisaniy ='+summa_spisaniy)
        # print('summa_popolneniy =' + summa_popolneniy)

        summa_popolneniy = get_float_from_money(summa_popolneniy)
        summa_spisaniy = get_float_from_money(summa_spisaniy)

        return summa_popolneniy - summa_spisaniy

    def split_text_on_entries(self)->list[str]:
        """
        разделяет текстовый файл формата 2107_Stavropol на отдельные записи

        пример одной записи
    ------------------------------------------------------------------------------------------------------
        03.07.2021 12:52 -> Перевод с карты -> 3 500,00 -> 28 655,30
        03.07.2021 123456 -> SBOL перевод 1234****1234 Н. ИГОРЬ РОМАНОВИЧ
    ------------------------------------------------------------------------------------------------------

        либо такой
    --------------------------------------------------------------------------------------------------
        28.06.2021 00:00 -> Неизвестная категория(+) -> +21107,75 -> 22113,73
        28.06.2021 - -> Прочие выплаты
    ----------------------------------------------------------------------------------------------------

        либо такой с иностранной вылютой
    ---------------------------------------------------------------------------------------------------------
        08.07.2021 18:27 -> Все для дома     193,91     14593,30
        09.07.2021 254718 -> XXXXX XXXXX -> 2,09 €
    ---------------------------------------------------------------------------------------------------------

        ещё один пример (с 3 линиями)
        ---------------------------------------------------------------------------------------------------------
        03.07.2021 11:54 -> Перевод с карты -> 4 720,00 -> 45 155,30
        03.07.2021 258077 -> SBOL перевод 1234****5678 А. ВАЛЕРИЯ
        ИГОРЕВНА
        ----------------------------------------------------------------------------------------------------------

        """
        # extracting entries (operations) from text file on
        individual_entries = re.findall(r"""
            \d\d\.\d\d\.\d\d\d\d\s{1}\d\d:\d\d                              # Date and time like '06.07.2021 15:46'                                        
            .*?\n                                                           # Anything till end of the line including a line break
            \d\d\.\d\d\.\d\d\d\d\s{1}                                       # дата обработки и 1 пробел 
            (?=\d{3,8}|-|0)                                                 # код авторизации, либо "-", либо 0 (issue 33). 
                                                                            # Код авторизациии который я видел всегда состоит и 6 цифр, но на всякий случай укажим с 3 до 8
            [\s\S]*?                                                        # any character, including new line. !!None-greedy!!
            (?=Продолжение\sна\sследующей\sстранице|                        # lookahead до "Продолжение на следующей странице"
             \d\d\.\d\d\.\d\d\d\d\s{1}\d\d:\d\d|                            # Либо до начала новой страницы
              Реквизиты\sдля\sперевода)                                     # Либо да конца выписки
            """,
                                        self.bank_text, re.VERBOSE)

        if len(individual_entries) == 0:
            raise exceptions.InputFileStructureError(
                "Не обнаружена ожидаемая структора данных: не найдено ни одной трасакции")

        # for entry in individual_entries:
        #     print(entry)

        return individual_entries

    def decompose_entry_to_dict(self, entry:str)->dict[str, Any]:
        """
        Выделяем данные из одной записи в dictionary

    ------------------------------------------------------------------------------------------------------
        03.07.2021 12:52 -> Перевод с карты -> 3 500,00 -> 28 655,30
        03.07.2021 123456 -> SBOL перевод 1234****1234 Н. ИГОРЬ РОМАНОВИЧ
    ------------------------------------------------------------------------------------------------------

        либо такой
    --------------------------------------------------------------------------------------------------
        28.06.2021 00:00 -> Неизвестная категория(+)     +21107,75     22113,73
        28.06.2021 - -> Прочие выплаты
    ----------------------------------------------------------------------------------------------------

        ещё один пример (с 3 линиями)
        ---------------------------------------------------------------------------------------------------------
        03.07.2021 11:54 -> Перевод с карты -> 4 720,00 -> 45 155,30
        03.07.2021 258077 -> SBOL перевод 1234****5678 А. ВАЛЕРИЯ
        ИГОРЕВНА
        ----------------------------------------------------------------------------------------------------------

        либо такой с иностранной вылютой
    ---------------------------------------------------------------------------------------------------------
        08.07.2021 18:27 -> Все для дома -> 193,91 -> 14593,30
        09.07.2021 -> 254718 -> XXXXX XXXXX -> 2,09 €
    ---------------------------------------------------------------------------------------------------------

        В последнем примере:

    {'authorisation_code': '254718',
     'category': 'Все для дома',
     'description': 'XXXXX XXXXX',
     'operation_date': '08.07.2021 18:27',
     'processing_date': '09.07.2021',
     'remainder_account_currency': 14593.30,
     'value_account_currency': -193.91б
     'operational_currency': '€'
     }
        """
        lines = entry.split('\n')
        lines = list(filter(None, lines))

        if len(lines) < 2 or len(lines) > 3:
            raise exceptions.InputFileStructureError(
                "entry is expected to have from 2 to 3 lines\n" + str(entry))

        result: dict = {}
        # ************** looking at the 1st line
        line_parts = split_Sberbank_line(lines[0])

        # print( f"1st line line_parts {line_parts}")

        result['operation_date'] = line_parts[0] + " " + line_parts[1]
        # https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior
        result['operation_date'] = datetime.strptime(result['operation_date'], '%d.%m.%Y %H:%M')

        result['category'] = line_parts[2]
        result['value_account_currency'] = get_float_from_money(line_parts[3], True)
        # result['remainder_account_currency'] = get_float_from_money(
        #     line_parts[4])

        # ************** looking at the 2nd line
        line_parts = split_Sberbank_line(lines[1])

        if len(line_parts) < 2 or len(line_parts) > 4:
            raise exceptions.Bank2ExcelError(
                "Line is expected to have 2 or 4 parts :" + str(lines[1]))

        # print(line_parts[0])

        # processing_date__authorisation_code = re.search(r'(dd\.dd\.dddd)\s(.*)', line_parts[0])
        result['processing_date'] = line_parts[0]
        # https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior
        result['processing_date'] = datetime.strptime(result['processing_date'], '%d.%m.%Y')

        result['authorisation_code'] = line_parts[1]
 
        # checking if the last part is a money like '1 515,76 €'
        last_part_as_money: re.Match | None = re.search(r'(\d[\d\s]*?,\d\d)\s(\S*)$', line_parts[-1])
        
        # checking if the last part is a currency like in issue_39:
        # 13.03.2024	814632	MAPP_SBERBANK_ONL@IN_PAY. Операция по карте ****XXX	₽
        last_part_as_currency: re.Match | None = re.search(r'(\S)$', line_parts[-1])   
             
        if len(line_parts) == 3:
            
            if last_part_as_money:
                
                # Обрабатываем вторую строчку в никогда не встречавшемся, но наверное теоретически возможном случае, когда 
                # во второй строке отсутствует описание, но присутствует сумма в иностранной валюте
                # https://github.com/Ev2geny/Sberbank2Excel/issues/36
                """
                     09.08.2022	21:46	Отдых и развлечения	140,04
                -->  11.08.2022	214722  6,00 BYN
                """
                
                result['value_operational_currency'] = get_float_from_money(last_part_as_money.group(1), True)
                result['operational_currency'] = last_part_as_money.group(2)
            else:
                # Обрабатываем вторую строчку "стандартной" трансакции
                """
                    06.08.2022	01:17	Отдых и развлечения	1 564,00
                --> 06.08.2022	291231	YANDEX.TAXI
                """
                result['description'] = line_parts[2]
            

        if len(line_parts) == 4:
            
            result['description'] = line_parts[2]
            
            if last_part_as_money:
                result['value_operational_currency'] = get_float_from_money(last_part_as_money.group(1), True)
                result['operational_currency'] = last_part_as_money.group(2)
            
            # Обрабатываем вот такую ситуацию (issue_39)
            # 13.03.2024	814632	MAPP_SBERBANK_ONL@IN_PAY. Операция по карте ****XXX	₽
            elif last_part_as_currency:
                result['operational_currency'] = last_part_as_currency.group(1)
                
            else:
                raise exceptions.InputFileStructureError(
                    f"Ошибка в обработке текста. Ожидалась структура типа '6,79 €', либо '₽' получено:  {line_parts[3]}")

        # ************** looking at the 3rd line, if present
        if len(lines) == 3:
            line_parts = split_Sberbank_line(lines[2])
            result['description'] = result['description'] + ' ' + line_parts[0]

        # print(result)

        return result

    def get_column_name_for_balance_calculation(self)->str:
        return 'value_account_currency'

    def get_columns_info(self)->dict:
        """
        Returns full column names in the order they shall appear in Excel
        The keys in dictionary shall correspond to keys of the result of the function self.decompose_entry_to_dict()
        """
        return {'operation_date': 'Дата операции',
                'processing_date': 'Дата обработки',
                'authorisation_code': 'Код авторизации',
                'description': 'Описание операции',
                'category': 'Категория',
                'value_account_currency': 'Сумма в валюте счёта',
                'value_operational_currency': 'Сумма в валюте операции',
                'operational_currency': 'Валюта операции'}


if __name__ == '__main__':


    if len(sys.argv) < 2:
        print('Не указано имя текстового файла для проверки экстрактора')
        print(__doc__)

    else:
        extractors_generic.debug_extractor(SBER_DEBIT_2212,
                                           test_text_file_name=sys.argv[1])