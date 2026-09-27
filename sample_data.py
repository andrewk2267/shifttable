"""Demo businesses for ShiftTable, one per industry, each with two branches.

Dates are placed relative to today so a demo always looks current: three past weeks, this
week and next week are published; the week after is a draft. Past shifts get realistic
clock records (a few late arrivals, early finishes, no-shows and unscheduled hours) so the
actual-vs-rostered report has something to compare. A few problems are planted so the
roster health panel and request queue have work to show.
"""
import random
from datetime import date, datetime, timedelta

import rules

# username, password, display name (matches an employee in every dataset), role, sign-in label
DEMO_USERS = [
    ('admin', 'Admin#2026', 'Grace Loh', 'admin', 'Owner (admin)'),
    ('manager', 'Manager#2026', 'Rachel Tan', 'manager', 'Manager'),
    ('marcus', 'Staff#2026', 'Marcus Wong', 'staff', 'Staff (full-time)'),
    ('priya', 'Staff#2026', 'Priya Nair', 'staff', 'Staff (part-time)'),
]

# Singapore gazetted public holidays (MOM press releases of 16 Jun 2025 and 18 Jun 2026).
# When a holiday falls on a Sunday the following Monday is the paid public holiday; the
# Sunday itself is kept as a calendar note.
PUBLIC_HOLIDAYS = {
    2026: [('2026-01-01', "New Year's Day"), ('2026-02-17', 'Chinese New Year'), ('2026-02-18', 'Chinese New Year (day 2)'),
           ('2026-03-21', 'Hari Raya Puasa'), ('2026-04-03', 'Good Friday'), ('2026-05-01', 'Labour Day'),
           ('2026-05-27', 'Hari Raya Haji'), ('2026-06-01', 'Vesak Day (observed)'), ('2026-08-10', 'National Day (observed)'),
           ('2026-11-09', 'Deepavali (observed)'), ('2026-12-25', 'Christmas Day')],
    2027: [('2027-01-01', "New Year's Day"), ('2027-02-06', 'Chinese New Year'), ('2027-02-08', 'Chinese New Year (observed)'),
           ('2027-03-10', 'Hari Raya Puasa'), ('2027-03-26', 'Good Friday'), ('2027-05-01', 'Labour Day'),
           ('2027-05-17', 'Hari Raya Haji'), ('2027-05-20', 'Vesak Day'), ('2027-08-09', 'National Day'),
           ('2027-10-28', 'Deepavali'), ('2027-12-25', 'Christmas Day')],
}
HOLIDAY_NOTES = {
    2026: [('2026-05-31', 'Vesak Day (holiday on Monday)'), ('2026-08-09', 'National Day (holiday on Monday)'),
           ('2026-11-08', 'Deepavali (holiday on Monday)')],
    2027: [('2027-02-07', 'Chinese New Year day 2 (holiday on Monday)')],
}


def holiday_events(year):
    """(date, kind, name) for one year: paid holidays plus Sunday notes."""
    return [(d, 'holiday', n) for d, n in PUBLIC_HOLIDAYS.get(year, [])] + [(d, 'event', n) for d, n in HOLIDAY_NOTES.get(year, [])]


ANY = (1, None, None)
OFF = (0, None, None)


def window(start, end):
    return (1, start, end)


def weekdays(av, days=range(5)):
    return {d: av for d in days}


# ---------------------------------------------------------------- F&B

def fnb_needs(b, wd):
    fri, wkend = wd == 4, wd >= 5
    if b == 0:  # Robertson Quay: all-day bistro and bar
        n = [('Supervisor', 'Opening', 1, 'Open')]
        if fri or wkend:
            n.append(('Supervisor', 'Closing', 1, 'Close'))
        n += [('Server', 'Opening', 2 if wkend else 1, 'Breakfast'), ('Server', 'Closing', 1, 'Dinner service')]
        if wkend:
            n.append(('Server', 'Lunch peak', 1, 'Brunch rush'))
        if fri or wkend:
            n.append(('Server', 'Dinner peak', 1, 'Dinner rush'))
        n.append(('Barista', 'Opening', 1, 'Coffee bar'))
        if wd == 5:
            n.append(('Barista', 'Mid', 1, 'Weekend coffee'))
        if wd >= 2:
            n.append(('Bartender', 'Closing', 1, 'Bar'))
        if 1 <= wd <= 5:
            n.append(('Head Chef', 'Kitchen AM', 1, 'Pass'))
        n += [('Line Cook', 'Kitchen AM', 1, 'Lunch line'), ('Line Cook', 'Kitchen PM', 2 if wd == 5 else 1, 'Dinner line'),
              ('Prep Cook', 'Kitchen AM', 1, 'Prep'), ('Kitchen Porter', 'Kitchen PM', 1, 'Wash-up')]
        if wd == 6:
            n.append(('Kitchen Porter', 'Kitchen AM', 1, 'Brunch wash-up'))
        return n
    n = [('Supervisor', 'Mid', 1, 'Duty manager'), ('Server', 'Opening', 1, 'Open'), ('Server', 'Closing', 1, 'Close'),
         ('Barista', 'Opening', 1, 'Coffee bar'), ('Line Cook', 'Kitchen AM', 1, 'Lunch line'), ('Line Cook', 'Kitchen PM', 1, 'Dinner line'),
         ('Kitchen Porter', 'Kitchen PM', 1, 'Wash-up')]
    if wkend:
        n.append(('Server', 'Lunch peak', 1, 'Mall lunch rush'))
    if fri or wkend:
        n.append(('Cashier', 'Mid', 1, 'Takeaway counter'))
    if wd >= 1:
        n.append(('Prep Cook', 'Kitchen AM', 1, 'Prep'))
    return n


EVENINGS = window('17:00', '23:59')

FNB = {
    'label': 'F&B: restaurant group',
    'description': 'Harbour Lane Group: a bistro-bar and a mall café, 30 staff, front of house and kitchen.',
    'business_name': 'Harbour Lane Group', 'forecast_label': 'Forecast sales', 'target_labour_pct': 30, 'domain': 'harbourlane.example',
    'branches': [('Robertson Quay', 'RQ', '#0e6b5c', '30 Robertson Quay #01-05'), ('Tampines Mall', 'TM', '#b45f1c', '4 Tampines Central 5 #02-11')],
    'positions': [('Supervisor', 'Front of house', '#6d5bd0'), ('Server', 'Front of house', '#2f7fd1'), ('Barista', 'Front of house', '#a1662f'),
                  ('Bartender', 'Front of house', '#c2417a'), ('Cashier', 'Front of house', '#5b8c2a'), ('Head Chef', 'Kitchen', '#d4552f'),
                  ('Line Cook', 'Kitchen', '#e08a1e'), ('Prep Cook', 'Kitchen', '#b8932a'), ('Kitchen Porter', 'Kitchen', '#6b7280')],
    'templates': {'Opening': ('07:30', '16:30', 60, 'Front of house'), 'Mid': ('11:00', '20:00', 60, 'Front of house'),
                  'Closing': ('14:00', '23:00', 60, 'Front of house'), 'Lunch peak': ('11:00', '15:00', 0, 'Front of house'),
                  'Dinner peak': ('17:30', '22:30', 0, 'Front of house'), 'Kitchen AM': ('07:00', '16:00', 60, 'Kitchen'),
                  'Kitchen PM': ('13:30', '22:30', 60, 'Kitchen')},
    # name, code, main position, type, rate, usual h, max h, other skills, home branch, other branches, availability
    'employees': [
        ('Rachel Tan', 'HL001', 'Supervisor', 'full_time', 17.0, 40, 48, ['Server', 'Cashier'], 0, [1], {}),
        ('Daniel Lim', 'HL002', 'Supervisor', 'full_time', 16.5, 40, 48, ['Server', 'Bartender'], 0, [], {}),
        ('Nur Aisyah Rahman', 'HL003', 'Server', 'full_time', 12.5, 40, 48, ['Cashier', 'Supervisor'], 0, [], {}),
        ('Marcus Wong', 'HL004', 'Server', 'full_time', 12.5, 40, 48, ['Bartender'], 0, [], {}),
        ('Priya Nair', 'HL005', 'Server', 'part_time', 11.0, 24, 30, ['Cashier'], 0, [1], {**weekdays(window('07:00', '17:00')), 6: OFF}),
        ('Jaden Koh', 'HL006', 'Server', 'part_time', 10.5, 16, 24, [], 0, [], weekdays(EVENINGS)),
        ('Sean Chia', 'HL007', 'Server', 'part_time', 10.5, 20, 28, [], 0, [], {2: OFF}),
        ('Ethan Ong', 'HL008', 'Barista', 'full_time', 13.0, 40, 48, ['Cashier'], 0, [], {}),
        ('Kavin Raj', 'HL009', 'Bartender', 'full_time', 13.5, 40, 48, ['Server'], 0, [], {}),
        ('Hafiz Ismail', 'HL010', 'Head Chef', 'full_time', 24.0, 40, 48, ['Line Cook'], 0, [1], {}),
        ('Wei Jie Ng', 'HL011', 'Line Cook', 'full_time', 15.5, 40, 48, ['Prep Cook', 'Head Chef'], 0, [], {}),
        ('Arjun Pillai', 'HL012', 'Line Cook', 'full_time', 14.5, 40, 48, ['Prep Cook'], 0, [1], {}),
        ('Lina Wati', 'HL013', 'Prep Cook', 'full_time', 12.0, 40, 48, ['Line Cook', 'Kitchen Porter'], 0, [], {}),
        ('Lee Ah Seng', 'HL014', 'Kitchen Porter', 'full_time', 10.0, 40, 48, ['Prep Cook'], 0, [], {}),
        ('Tommy Lau', 'HL027', 'Line Cook', 'full_time', 14.0, 40, 48, ['Prep Cook'], 0, [], {}),
        ('Nisha Rao', 'HL028', 'Prep Cook', 'part_time', 11.0, 24, 30, ['Kitchen Porter', 'Line Cook'], 0, [1], {}),
        ('Amelia Goh', 'HL029', 'Server', 'casual', 10.5, 16, 24, ['Barista'], 0, [], {0: OFF, 1: OFF, 2: OFF, 3: OFF}),
        ('Darren Lee', 'HL030', 'Prep Cook', 'casual', 10.5, 16, 24, ['Kitchen Porter', 'Line Cook'], 0, [], {0: OFF, 1: OFF, 2: OFF, 3: OFF}),
        ('Aminah Yusof', 'HL015', 'Supervisor', 'full_time', 16.0, 40, 48, ['Server', 'Cashier'], 1, [], {}),
        ('Brandon Ho', 'HL016', 'Server', 'full_time', 12.0, 40, 48, ['Cashier'], 1, [], {}),
        ('Farah Hassan', 'HL017', 'Server', 'casual', 10.5, 12, 24, ['Cashier'], 1, [0], {0: OFF, 1: OFF, 2: OFF, 3: OFF}),
        ('Ivan Koh', 'HL018', 'Server', 'part_time', 10.5, 20, 28, [], 1, [], {}),
        ('Chloe Teo', 'HL019', 'Barista', 'part_time', 11.5, 24, 30, ['Server'], 1, [0], {1: OFF, 2: OFF}),
        ('Kelvin Sim', 'HL020', 'Barista', 'full_time', 12.5, 40, 48, ['Cashier'], 1, [], {}),
        ('Mei Ling Chua', 'HL021', 'Cashier', 'part_time', 10.5, 20, 28, ['Server'], 1, [], {6: OFF}),
        ('Ravi Kumar', 'HL022', 'Line Cook', 'full_time', 15.0, 40, 48, ['Head Chef', 'Prep Cook'], 1, [], {}),
        ('Bryan Goh', 'HL023', 'Line Cook', 'part_time', 13.0, 24, 30, ['Prep Cook'], 1, [0], {0: OFF}),
        ('Hui Min Lau', 'HL024', 'Prep Cook', 'part_time', 11.0, 20, 28, ['Kitchen Porter'], 1, [], {3: OFF}),
        ('Joyce Yeo', 'HL025', 'Kitchen Porter', 'part_time', 10.0, 20, 28, [], 1, [], {5: OFF}),
        ('Siva Raman', 'HL026', 'Kitchen Porter', 'full_time', 10.0, 40, 48, ['Prep Cook'], 1, [0], {}),
    ],
    'notes': {'Jaden Koh': 'Student: evenings only on weekdays.', 'Amelia Goh': 'Weekend casual (university student).', 'Darren Lee': 'Weekend casual.', 'Priya Nair': 'School run after 5pm on weekdays.',
              'Hafiz Ismail': 'Covers both kitchens. Food hygiene cert renews in March.'},
    'needs': fnb_needs,
    'sales': {0: [3450, 3600, 3700, 3950, 4800, 6000, 5300], 1: [2450, 2400, 2500, 2650, 3150, 3900, 3700]},
    # week offset, weekday, days, kind, name, branch index (None = all)
    'events': [(0, 4, 1, 'event', 'Private dinner booking: 40 pax', 0), (-1, 5, 1, 'event', 'Mall roadshow, expect high footfall', 1),
               (1, 0, 7, 'event', 'Oktoberfest menu promotion', 0), (2, 0, 1, 'closed', 'Mall closed for M&E maintenance', 1)],
    'plant': {'clopen': 'Daniel Lim', 'late': 'Closing', 'early': 'Opening', 'mc': 'Ethan Ong', 'drop': 'Jaden Koh', 'cover_to': 'Kavin Raj',
              'leave': [('Chloe Teo', -1, 3, 2, 'approved', "Cousin's wedding"), ('Bryan Goh', 1, 1, 2, 'approved', 'Moving house'),
                        ('Joyce Yeo', 1, 6, 1, 'rejected', 'Birthday')]},
}


# ---------------------------------------------------------------- Health & Wellness

def wellness_needs(b, wd):
    sat, sun = wd == 5, wd == 6
    if b == 0:  # Novena: physiotherapy, spa and gym
        if sat:
            return [('Centre Manager', 'Weekend', 1, 'Duty manager'), ('Front Desk', 'Weekend', 2, 'Reception'), ('Physiotherapist', 'Weekend', 1, 'Clinic'),
                    ('Massage Therapist', 'Weekend', 2, 'Spa'), ('Fitness Coach', 'Weekend', 1, 'Gym floor'), ('Housekeeping', 'Weekend', 1, 'Housekeeping')]
        if sun:
            return [('Front Desk', 'Weekend', 1, 'Reception'), ('Physiotherapist', 'Weekend', 1, 'Clinic'), ('Massage Therapist', 'Weekend', 2, 'Spa'),
                    ('Fitness Coach', 'Weekend', 1, 'Gym floor'), ('Housekeeping', 'Weekend', 1, 'Housekeeping')]
        n = [('Centre Manager', 'Early', 1, 'Duty manager'), ('Front Desk', 'Early', 1, 'Reception AM'), ('Front Desk', 'Late', 1, 'Reception PM'),
             ('Physiotherapist', 'Early', 1, 'Clinic AM'), ('Physiotherapist', 'Late', 1, 'Clinic PM'), ('Fitness Coach', 'Early', 1, 'Gym AM'),
             ('Fitness Coach', 'Evening', 1, 'After-work classes'), ('Housekeeping', 'Mid', 1, 'Housekeeping')]
        n.append(('Massage Therapist', 'Late', 2, 'Friday spa') if wd == 4 else ('Massage Therapist', 'Mid', 1, 'Spa'))
        return n
    if sat:
        return [('Front Desk', 'Weekend', 1, 'Reception'), ('Physiotherapist', 'Weekend', 1, 'Clinic'), ('Massage Therapist', 'Weekend', 2, 'Spa'),
                ('Housekeeping', 'Weekend', 1, 'Housekeeping')]
    if sun:
        return [('Front Desk', 'Weekend', 1, 'Reception'), ('Massage Therapist', 'Weekend', 2, 'Spa'), ('Housekeeping', 'Weekend', 1, 'Housekeeping')]
    n = [('Front Desk', 'Early', 1, 'Reception AM'), ('Physiotherapist', 'Early', 1, 'Clinic'),
         ('Massage Therapist', 'Mid', 1, 'Spa'), ('Housekeeping', 'Mid', 1, 'Housekeeping')]
    if wd >= 2:
        n.append(('Front Desk', 'Late', 1, 'Reception PM'))
    if wd == 4:
        n.append(('Massage Therapist', 'Late', 1, 'Friday spa'))
    return n


WELLNESS = {
    'label': 'Health & Wellness: clinics and spas',
    'description': 'Vitality Wellness: physiotherapy, spa and gym at two centres, 22 staff.',
    'business_name': 'Vitality Wellness', 'forecast_label': 'Forecast revenue', 'target_labour_pct': 40, 'domain': 'vitality.example',
    'branches': [('Novena', 'NOV', '#0e6b5c', '10 Sinaran Drive #03-18'), ('Jurong East', 'JE', '#7a4fb3', '50 Jurong Gateway Road #04-02')],
    'positions': [('Centre Manager', 'Management', '#6d5bd0'), ('Front Desk', 'Front desk', '#2f7fd1'), ('Physiotherapist', 'Clinical', '#0f8b8d'),
                  ('Massage Therapist', 'Spa', '#c2417a'), ('Fitness Coach', 'Fitness', '#e08a1e'), ('Housekeeping', 'Support', '#6b7280')],
    'templates': {'Early': ('08:00', '17:00', 60, ''), 'Mid': ('10:30', '19:30', 60, ''), 'Late': ('12:30', '21:30', 60, ''),
                  'Weekend': ('09:00', '18:00', 60, ''), 'Evening': ('17:00', '21:30', 0, '')},
    'employees': [
        ('Rachel Tan', 'VW001', 'Centre Manager', 'full_time', 22.0, 40, 48, ['Front Desk'], 0, [1], {6: OFF}),
        ('Joanne Lau', 'VW002', 'Front Desk', 'full_time', 12.0, 40, 48, [], 0, [], {}),
        ('Hannah Ng', 'VW003', 'Front Desk', 'part_time', 11.0, 20, 28, [], 0, [], {0: OFF, 1: OFF}),
        ('Daniel Chua', 'VW004', 'Front Desk', 'part_time', 11.0, 20, 28, ['Housekeeping'], 0, [1], {}),
        ('Aaron Lim', 'VW005', 'Physiotherapist', 'full_time', 28.0, 40, 48, [], 0, [], {}),
        ('Priya Nair', 'VW006', 'Physiotherapist', 'part_time', 27.0, 24, 30, [], 0, [1], {**weekdays(window('08:00', '17:30')), 6: OFF}),
        ('Mei Hua Zhang', 'VW007', 'Massage Therapist', 'full_time', 15.0, 40, 48, [], 0, [], {}),
        ('Anita Devi', 'VW008', 'Massage Therapist', 'full_time', 15.0, 40, 48, [], 0, [], {}),
        ('Wendy Chong', 'VW009', 'Massage Therapist', 'part_time', 14.0, 20, 28, [], 0, [], {2: OFF}),
        ('Marcus Wong', 'VW010', 'Fitness Coach', 'full_time', 18.0, 40, 48, [], 0, [], {}),
        ('Suresh Menon', 'VW011', 'Fitness Coach', 'part_time', 17.0, 16, 24, [], 0, [], weekdays(window('16:00', '21:30'))),
        ('Mary Goh', 'VW012', 'Housekeeping', 'full_time', 10.0, 40, 48, [], 0, [1], {}),
        ('Nicole Wee', 'VW013', 'Physiotherapist', 'full_time', 28.0, 40, 48, [], 1, [0], {}),
        ('Farid Salleh', 'VW014', 'Front Desk', 'full_time', 12.0, 40, 48, [], 1, [], {}),
        ('Emily Tay', 'VW015', 'Front Desk', 'part_time', 11.0, 20, 28, [], 1, [], {3: OFF}),
        ('Rina Sari', 'VW016', 'Massage Therapist', 'full_time', 14.5, 40, 48, [], 1, [], {}),
        ('Nadia Hamid', 'VW017', 'Massage Therapist', 'full_time', 14.5, 40, 48, [], 1, [0], {}),
        ('Lily Tan', 'VW018', 'Massage Therapist', 'part_time', 13.5, 20, 28, [], 1, [], {}),
        ('Sam Ho', 'VW019', 'Housekeeping', 'part_time', 10.0, 24, 30, [], 1, [], {}),
        ('Kevin Tan', 'VW020', 'Physiotherapist', 'full_time', 27.0, 40, 48, [], 1, [0], {}),
        ('Vanessa Lim', 'VW021', 'Front Desk', 'full_time', 12.0, 40, 48, [], 0, [1], {}),
        ('Rosnah Ali', 'VW022', 'Housekeeping', 'part_time', 10.0, 24, 30, [], 1, [0], {}),
    ],
    'notes': {'Suresh Menon': 'Personal trainer by day elsewhere: evening classes only.', 'Priya Nair': 'Part-time: weekdays until 5.30pm.'},
    'needs': wellness_needs,
    'sales': {0: [2750, 2650, 2750, 2850, 3350, 4400, 3050], 1: [1200, 1100, 1200, 1250, 1600, 2000, 1600]},
    'events': [(0, 3, 1, 'event', 'Corporate wellness day at client office', 0), (1, 5, 1, 'event', 'Open house: free posture screening', 1),
               (2, 6, 1, 'closed', 'Closed for renovation works', 1)],
    'plant': {'clopen': 'Joanne Lau', 'late': 'Late', 'early': 'Early', 'mc': 'Anita Devi', 'drop': 'Hannah Ng', 'cover_to': 'Suresh Menon',
              'leave': [('Wendy Chong', -1, 3, 1, 'approved', 'Family event'), ('Lily Tan', 1, 1, 2, 'approved', 'Course'),
                        ('Emily Tay', 1, 5, 1, 'rejected', 'Concert')]},
}


# ---------------------------------------------------------------- Manufacturing

def manufacturing_needs(b, wd):
    if b == 0:  # Tuas: three shifts on weekdays, a Saturday half-day
        if wd == 5:
            return [('Production Supervisor', 'Sat half-day', 1, 'Saturday run'), ('Machine Operator', 'Sat half-day', 2, 'Saturday run')]
        if wd == 6:
            return []
        return [('Production Supervisor', 'Morning', 1, 'Line lead'), ('Machine Operator', 'Morning', 3, 'CNC cells 1–3'),
                ('Maintenance Technician', 'Morning', 1, 'Maintenance'), ('QC Inspector', 'Morning', 1, 'QC'), ('Warehouse Operator', 'Office', 1, 'Receiving'),
                ('Production Supervisor', 'Afternoon', 1, 'Line lead'), ('Machine Operator', 'Afternoon', 3, 'CNC cells 1–3'),
                ('Maintenance Technician', 'Afternoon', 1, 'Maintenance'), ('QC Inspector', 'Afternoon', 1, 'QC'),
                ('Production Supervisor', 'Night', 1, 'Line lead'), ('Machine Operator', 'Night', 2, 'CNC cells 1–2'),
                ('Maintenance Technician', 'Night', 1, 'Maintenance')]
    if wd >= 5:
        return []
    return [('Production Supervisor', 'Morning', 1, 'Line lead'), ('Machine Operator', 'Morning', 2, 'Assembly'), ('QC Inspector', 'Morning', 1, 'QC'),
            ('Warehouse Operator', 'Office', 1, 'Dispatch'), ('Packer', 'Morning', 1, 'Packing'),
            ('Machine Operator', 'Afternoon', 2, 'Assembly'), ('Packer', 'Afternoon', 1, 'Packing')]


MANUFACTURING = {
    'label': 'Manufacturing: two plants',
    'description': 'Apex Precision Components: a 3-shift plant and a 2-shift plant, 30 staff.',
    'business_name': 'Apex Precision Components', 'forecast_label': 'Planned output value', 'target_labour_pct': 18, 'domain': 'apexprecision.example',
    'branches': [('Tuas Plant', 'TUAS', '#0e6b5c', '21 Tuas Avenue 7'), ('Woodlands Plant', 'WDL', '#2f6fb0', '8 Woodlands Loop #05-01')],
    'positions': [('Production Supervisor', 'Production', '#6d5bd0'), ('Machine Operator', 'Production', '#2f7fd1'),
                  ('Maintenance Technician', 'Maintenance', '#d4552f'), ('QC Inspector', 'Quality', '#0f8b8d'),
                  ('Warehouse Operator', 'Warehouse', '#a1662f'), ('Packer', 'Warehouse', '#b8932a')],
    'templates': {'Morning': ('07:00', '15:00', 45, ''), 'Afternoon': ('15:00', '23:00', 45, ''), 'Night': ('23:00', '07:00', 45, ''),
                  'Office': ('08:30', '17:30', 60, ''), 'Sat half-day': ('07:00', '12:00', 0, '')},
    'employees': [
        ('Rachel Tan', 'AP001', 'Production Supervisor', 'full_time', 20.0, 37, 48, ['QC Inspector'], 0, [1], {6: OFF}),
        ('Muhammad Firdaus', 'AP002', 'Production Supervisor', 'full_time', 19.0, 37, 48, ['Machine Operator'], 0, [], {}),
        ('Tan Wei Ming', 'AP003', 'Production Supervisor', 'full_time', 19.0, 37, 48, ['Machine Operator'], 0, [], {}),
        ('Marcus Wong', 'AP004', 'Machine Operator', 'full_time', 13.5, 37, 48, [], 0, [], {}),
        ('Arun Kumar', 'AP005', 'Machine Operator', 'full_time', 13.0, 37, 48, [], 0, [], {}),
        ('Zhang Lei', 'AP006', 'Machine Operator', 'full_time', 13.0, 37, 48, ['Warehouse Operator'], 0, [], {}),
        ('Rahim Uddin', 'AP007', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 0, [], {}),
        ('Kyaw Zin Oo', 'AP008', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 0, [], {}),
        ('Lim Boon Heng', 'AP009', 'Machine Operator', 'full_time', 13.5, 37, 48, ['Production Supervisor'], 0, [], {}),
        ('Wong Kah Wai', 'AP010', 'Machine Operator', 'full_time', 13.0, 37, 48, [], 0, [1], {}),
        ('Suresh Raman', 'AP011', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 0, [], {}),
        ('Ahmad Zulkifli', 'AP012', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 0, [], {}),
        ('Goh Chee Keong', 'AP013', 'Maintenance Technician', 'full_time', 17.0, 37, 48, [], 0, [1], {}),
        ('Raj Menon', 'AP014', 'Maintenance Technician', 'full_time', 16.5, 37, 48, [], 0, [], {}),
        ('Tay Kok Leong', 'AP015', 'Maintenance Technician', 'full_time', 16.5, 37, 48, [], 0, [], {}),
        ('Priya Nair', 'AP016', 'QC Inspector', 'part_time', 14.0, 24, 30, [], 0, [1], {**weekdays(window('07:00', '16:00')), 5: OFF, 6: OFF}),
        ('Jasmine Ong', 'AP017', 'QC Inspector', 'full_time', 14.5, 37, 48, [], 0, [], {}),
        ('Hassan Basri', 'AP018', 'QC Inspector', 'full_time', 14.0, 37, 48, [], 0, [], {}),
        ('Ong Teck Seng', 'AP019', 'Warehouse Operator', 'full_time', 12.5, 37, 48, ['Packer'], 0, [], {}),
        ('Lee Siew Ling', 'AP020', 'Production Supervisor', 'full_time', 19.0, 37, 48, ['QC Inspector'], 1, [], {}),
        ('Bala Krishnan', 'AP021', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 1, [0], {}),
        ('Chen Jia Hui', 'AP022', 'Machine Operator', 'full_time', 12.5, 37, 48, ['Packer'], 1, [], {}),
        ('Aung Myo Min', 'AP023', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 1, [0], {}),
        ('Tan Soo Kiat', 'AP024', 'Machine Operator', 'full_time', 13.0, 37, 48, ['Production Supervisor'], 1, [0], {}),
        ('Irfan Hakim', 'AP025', 'Machine Operator', 'full_time', 12.5, 37, 48, [], 1, [0], {}),
        ('Nurul Huda', 'AP026', 'QC Inspector', 'full_time', 14.0, 37, 48, ['Packer'], 1, [], {}),
        ('Wong Mun Kit', 'AP027', 'Warehouse Operator', 'full_time', 12.5, 37, 48, ['Packer'], 1, [0], {}),
        ('Siti Rohani', 'AP028', 'Packer', 'part_time', 11.0, 24, 30, [], 1, [], {5: OFF, 6: OFF}),
        ('Loh Mei Yee', 'AP029', 'Packer', 'part_time', 11.0, 24, 30, [], 1, [], {}),
        ('Kumar Selvam', 'AP030', 'Packer', 'part_time', 11.0, 20, 28, ['Warehouse Operator'], 1, [], {}),
    ],
    'notes': {'Priya Nair': 'Part-time QC: weekday mornings only.', 'Goh Chee Keong': 'Senior technician, covers both plants.'},
    'needs': manufacturing_needs,
    'sales': {0: [11000, 11000, 11000, 11000, 11000, 3500, 0], 1: [5500, 5500, 5500, 5500, 5500, 0, 0]},
    'events': [(0, 2, 1, 'event', 'Customer quality audit', 0), (1, 4, 1, 'event', 'Toolbox safety briefing, all shifts', None),
               (2, 4, 1, 'closed', 'Planned maintenance shutdown', 1)],
    'plant': {'clopen': 'Tan Wei Ming', 'late': 'Afternoon', 'early': 'Morning', 'mc': 'Zhang Lei', 'drop': 'Loh Mei Yee', 'cover_to': 'Arun Kumar',
              'leave': [('Kyaw Zin Oo', -1, 1, 3, 'approved', 'Home leave'), ('Chen Jia Hui', 1, 3, 2, 'approved', 'Family matter'),
                        ('Kumar Selvam', 1, 4, 1, 'rejected', 'Personal')]},
}

INDUSTRIES = {'fnb': FNB, 'wellness': WELLNESS, 'manufacturing': MANUFACTURING}


def catalogue():
    return [{'id': k, 'label': v['label'], 'description': v['description'], 'business_name': v['business_name'],
             'employees': len(v['employees']), 'branches': len(v['branches'])} for k, v in INDUSTRIES.items()]


BUSINESS_TABLES = ['attendance', 'shift_requests', 'leave_requests', 'forecasts', 'calendar_events', 'shifts', 'staffing_needs',
                   'shift_templates', 'availability', 'employee_skills', 'employee_branches', 'employees', 'positions', 'branches']


def _stamp(d, hhmm='10:00'):
    return f'{d.isoformat()} {hhmm}:00'


def _set(conn, key, value):
    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))


def load(conn, today, load_ctx, keep_users=True, industry='fnb', now=None):
    """Replace all business data with a demo business. Logins are kept and relinked by name."""
    ds = INDUSTRIES[industry]
    now = now or datetime.now()
    links = {}
    if keep_users:
        for uid, name in conn.execute('SELECT users.id, employees.name FROM users JOIN employees ON employees.id = users.employee_id'):
            links[uid] = name
    conn.execute('UPDATE users SET employee_id = NULL')
    for t in BUSINESS_TABLES:
        conn.execute(f'DELETE FROM {t}')
    for key in ('business_name', 'forecast_label', 'target_labour_pct'):
        _set(conn, key, ds[key])

    rnd = random.Random(f'shifttable-{industry}')
    monday = rules.monday_of(today)
    created = _stamp(monday - timedelta(days=60))

    br = [conn.execute('INSERT INTO branches (name, code, address, color, sort, active) VALUES (?,?,?,?,?,1)', (name, code, addr, color, i)).lastrowid
          for i, (name, code, color, addr) in enumerate(ds['branches'])]
    pos = {}
    for i, (name, dept, color) in enumerate(ds['positions']):
        pos[name] = conn.execute('INSERT INTO positions (name, department, color, sort) VALUES (?,?,?,?)', (name, dept, color, i)).lastrowid
    for i, (name, (s, e, b, dept)) in enumerate(ds['templates'].items()):
        conn.execute('INSERT INTO shift_templates (name, start_time, end_time, break_minutes, department, sort) VALUES (?,?,?,?,?,?)', (name, s, e, b, dept, i))

    emp, avail_of = {}, {}
    for name, code, main, etype, rate, target, mx, skills, home, others, avail in ds['employees']:
        email = name.lower().replace(' ', '.') + '@' + ds['domain']
        phone = f'9{rnd.randint(100, 999)} {rnd.randint(1000, 9999)}'
        eid = conn.execute('INSERT INTO employees (name, code, position_id, branch_id, employment_type, hourly_rate, target_hours, max_hours, phone, email, notes, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)',
                           (name, code, pos[main], br[home], etype, rate, target, mx, phone, email, ds['notes'].get(name, ''), created)).lastrowid
        emp[name] = eid
        for sk in {main, *skills}:
            conn.execute('INSERT INTO employee_skills (employee_id, position_id) VALUES (?,?)', (eid, pos[sk]))
        for o in others:
            conn.execute('INSERT INTO employee_branches (employee_id, branch_id) VALUES (?,?)', (eid, br[o]))
        avail_of[name] = {}
        for wd in range(7):
            a, s, e = avail.get(wd, ANY)
            conn.execute('INSERT INTO availability (employee_id, weekday, available, start_time, end_time) VALUES (?,?,?,?,?)', (eid, wd, a, s, e))
            avail_of[name][wd] = {'available': a, 'start_time': s, 'end_time': e}

    for bi, b in enumerate(br):
        for wd in range(7):
            for pname, tname, count, label in ds['needs'](bi, wd):
                s, e, brk, _d = ds['templates'][tname]
                conn.execute('INSERT INTO staffing_needs (branch_id, weekday, position_id, start_time, end_time, break_minutes, headcount, label) VALUES (?,?,?,?,?,?,?,?)',
                             (b, wd, pos[pname], s, e, brk, count, label))

    weeks = [monday + timedelta(days=7 * k) for k in range(-3, 3)]
    for bi, b in enumerate(br):
        for ws in weeks:
            for i in range(7):
                base = ds['sales'][bi][i]
                if base:
                    conn.execute('INSERT INTO forecasts (branch_id, date, sales, note) VALUES (?,?,?,?)',
                                 (b, (ws + timedelta(days=i)).isoformat(), round(base * rnd.uniform(0.92, 1.08), -1), ''))

    for year in (today.year, today.year + 1):
        for d, kind, name in holiday_events(year):
            conn.execute("INSERT INTO calendar_events (start_date, end_date, kind, name, branch_id, notes) VALUES (?,?,?,?,NULL,'Singapore public holiday')" if kind == 'holiday'
                         else "INSERT INTO calendar_events (start_date, end_date, kind, name, branch_id, notes) VALUES (?,?,?,?,NULL,'')", (d, d, kind, name))
    for wk, wd, length, kind, name, bi in ds['events']:
        start = monday + timedelta(days=7 * wk + wd)
        conn.execute('INSERT INTO calendar_events (start_date, end_date, kind, name, branch_id, notes) VALUES (?,?,?,?,?,?)',
                     (start.isoformat(), (start + timedelta(days=length - 1)).isoformat(), kind, name, None if bi is None else br[bi], ''))

    def leave(name, start, end, kind, status, reason=''):
        return conn.execute('INSERT INTO leave_requests (employee_id, start_date, end_date, kind, reason, status, created_by, decided_by, decided_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (emp[name], start.isoformat(), end.isoformat(), kind, reason, status, None, None,
                             _stamp(start - timedelta(days=12)) if status != 'pending' else None, _stamp(start - timedelta(days=20)))).lastrowid

    plant = ds['plant']
    for name, wk, wd, length, status, reason in plant['leave']:
        start = monday + timedelta(days=7 * wk + wd)
        leave(name, start, start + timedelta(days=length - 1), 'Annual leave', status, reason)

    w0, w1, w2 = weeks[3], weeks[4], weeks[5]
    for ws in weeks:
        ctx = load_ctx(conn, min(ws - timedelta(days=13), ws.replace(day=1)).isoformat(), (ws + timedelta(days=7)).isoformat())
        new, _ = rules.autofill(ctx, ws.isoformat())
        published = ws <= w1
        pub_at = _stamp(ws - timedelta(days=9), '16:00') if published else None
        for s in new:
            conn.execute('INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, branch_id, notes, status, published_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (s['date'], s['start_time'], s['end_time'], s['break_minutes'], s['employee_id'], s['position_id'], s['branch_id'], '',
                          'published' if published else 'draft', pub_at, _stamp(ws - timedelta(days=10)), _stamp(ws - timedelta(days=10))))

    def shifts_of(name, ws):
        days = rules.week_dates(ws.isoformat())
        return [dict(r) for r in conn.execute('SELECT * FROM shifts WHERE employee_id = ? AND date BETWEEN ? AND ? ORDER BY date, start_time',
                                              (emp[name], days[0], days[6]))]

    def touch(sid, **fields):
        fields.setdefault('changed_after_publish', 1)
        fields['updated_at'] = _stamp(today, '09:00')
        sets = ', '.join(f'{k} = ?' for k in fields)
        conn.execute(f'UPDATE shifts SET {sets} WHERE id = ?', (*fields.values(), sid))

    tpl = ds['templates']
    # 1. A clopen this week: someone moved to finish late one night and start early the next morning.
    cs = shifts_of(plant['clopen'], w0)
    for a, b in zip(cs, cs[1:]):
        if (date.fromisoformat(b['date']) - date.fromisoformat(a['date'])).days == 1:
            late, early = tpl[plant['late']], tpl[plant['early']]
            touch(a['id'], start_time=late[0], end_time=late[1])
            touch(b['id'], start_time=early[0], end_time=early[1])
            break
    # 2. Medical leave approved after the roster went out: the shift still needs cover.
    ms = shifts_of(plant['mc'], w0)
    if ms:
        d = date.fromisoformat(ms[min(2, len(ms) - 1)]['date'])
        leave(plant['mc'], d, d, 'Medical leave', 'approved', 'MC from polyclinic')
    # 3. A release approved this week: the shift is now open.
    ds_ = shifts_of(plant['drop'], w0)
    if ds_:
        s = ds_[-1]
        conn.execute("INSERT INTO shift_requests (kind, shift_id, from_employee_id, to_employee_id, note, status, created_by, decided_by, decided_at, created_at) VALUES ('drop',?,?,NULL,?, 'approved', NULL, NULL, ?, ?)",
                     (s['id'], s['employee_id'], 'Exam moved to that day', _stamp(today - timedelta(days=2), '11:00'), _stamp(today - timedelta(days=3), '20:15')))
        touch(s['id'], employee_id=None)
    # 4. Next week: Marcus asks a colleague to cover, and another of his shifts moved after publishing.
    mk = shifts_of('Marcus Wong', w1)
    if mk:
        conn.execute("INSERT INTO shift_requests (kind, shift_id, from_employee_id, to_employee_id, note, status, created_by, created_at) VALUES ('cover',?,?,?,?, 'pending', NULL, ?)",
                     (mk[0]['id'], emp['Marcus Wong'], emp[plant['cover_to']], f"Family dinner. {plant['cover_to'].split()[0]} said he's OK to take it.", _stamp(today, '08:40')))
    if len(mk) > 1:
        s = mk[1]
        st, en = rules.mins(s['start_time']) + 30, rules.mins(s['end_time']) + 30
        touch(s['id'], start_time=f'{st // 60 % 24:02d}:{st % 60:02d}', end_time=f'{en // 60 % 24:02d}:{en % 60:02d}')
    # 5. Next week: a shift freed up and Priya wants to pick it up.
    priya = next(e for e in ds['employees'] if e[0] == 'Priya Nair')
    p_skills = {pos[priya[2]], *(pos[x] for x in priya[7])}
    p_branches = {br[priya[8]], *(br[o] for o in priya[9])}
    p_days = {s['date'] for s in shifts_of('Priya Nair', w1)}
    p_emp = {'availability': avail_of['Priya Nair']}
    w1_days = rules.week_dates(w1.isoformat())
    for r in conn.execute('SELECT * FROM shifts WHERE date BETWEEN ? AND ? AND employee_id IS NOT NULL AND employee_id NOT IN (?, ?) ORDER BY date DESC, start_time',
                          (w1_days[0], w1_days[5], emp['Priya Nair'], emp['Marcus Wong'])).fetchall():
        r = dict(r)
        if r['position_id'] in p_skills and r['branch_id'] in p_branches and r['date'] not in p_days and not rules.availability_problem(p_emp, r):
            touch(r['id'], employee_id=None)
            conn.execute("INSERT INTO shift_requests (kind, shift_id, from_employee_id, to_employee_id, note, status, created_by, created_at) VALUES ('pickup',?,NULL,?,?, 'pending', NULL, ?)",
                         (r['id'], emp['Priya Nair'], 'Happy to take this one.', _stamp(today, '10:05')))
            break
    # 6. A leave request waiting for a decision in the draft week.
    leave('Priya Nair', w2 + timedelta(days=3), w2 + timedelta(days=4), 'Annual leave', 'pending', 'Family trip')

    make_attendance(conn, rnd, weeks[0], today, now)

    for uid, name in links.items():
        if name in emp:
            conn.execute('UPDATE users SET employee_id = ? WHERE id = ?', (emp[name], uid))
    for uid, display in conn.execute('SELECT id, display_name FROM users WHERE employee_id IS NULL').fetchall():
        if display in emp:
            conn.execute('UPDATE users SET employee_id = ? WHERE id = ?', (emp[display], uid))
    return emp


def make_attendance(conn, rnd, start, today, now):
    """Clock records for past published shifts: mostly on time, some late or early,
    a few no-shows, missing clock-outs and unscheduled extra hours."""
    now_min = today.toordinal() * rules.DAY + now.hour * 60 + now.minute
    stamp = lambda m: f'{date.fromordinal(m // rules.DAY).isoformat()} {rules.fmt_clock(m)}'
    rows = [dict(r) for r in conn.execute("SELECT * FROM shifts WHERE status = 'published' AND employee_id IS NOT NULL AND date BETWEEN ? AND ? ORDER BY date, start_time",
                                          (start.isoformat(), today.isoformat()))]
    worked = {(s['employee_id'], s['date']) for s in rows}
    created = _stamp(today, '06:00')
    for s in rows:
        a, b = rules.span(s)
        if a > now_min:
            continue
        roll = rnd.random()
        if roll < 0.03:
            continue  # no-show
        cin = a + rnd.randint(6, 28) if roll < 0.13 else a - rnd.randint(2, 12)
        if b > now_min:
            cout = None  # still on shift
        elif rnd.random() < 0.01:
            cout = None  # forgot to clock out
        elif rnd.random() < 0.06:
            cout = b - rnd.randint(12, 45)
        else:
            cout = b + rnd.randint(0, 20)
        conn.execute("INSERT INTO attendance (employee_id, branch_id, date, clock_in, clock_out, break_minutes, source, created_at) VALUES (?,?,?,?,?,?,'Sample time clock',?)",
                     (s['employee_id'], s['branch_id'], s['date'], stamp(cin), stamp(cout) if cout else None, s['break_minutes'], created))
    people = sorted({s['employee_id'] for s in rows})
    d = start
    while d < today:
        if people and rnd.random() < 0.35:
            e = rnd.choice(people)
            if (e, d.isoformat()) not in worked:
                home = conn.execute('SELECT branch_id FROM employees WHERE id = ?', (e,)).fetchone()[0]
                cin = d.toordinal() * rules.DAY + 10 * 60 + rnd.randint(0, 60)
                conn.execute("INSERT INTO attendance (employee_id, branch_id, date, clock_in, clock_out, break_minutes, source, created_at) VALUES (?,?,?,?,?,0,'Sample time clock',?)",
                             (e, home, d.isoformat(), stamp(cin), stamp(cin + 4 * 60 + rnd.randint(0, 30)), created))
        d += timedelta(days=1)
