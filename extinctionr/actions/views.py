import calendar
import csv
import math

from collections import defaultdict
from datetime import timedelta, datetime
from dateutil import tz
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core import signing
from django.db import IntegrityError
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, get_object_or_404, redirect
from django.utils.html import strip_tags
from django.utils.http import http_date
from django.utils.timezone import now, localtime
from django.urls import reverse
from django.views.decorators.cache import never_cache, cache_page
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from django import forms
from phonenumber_field.formfields import PhoneNumberField

from extinctionr.utils import get_last_contact, set_last_contact, get_contact
from .models import Action, ActionRole, Attendee, TalkProposal
from .comm import notify_commitments


BOOTSTRAP_ATTRS = {'class': 'form-control text-center'}

class ActionForm(forms.ModelForm):
    class Meta:
        model = Action
        fields = ('name', 'when', 'description', 'public', 'location', 'tags', 'slug', 'accessibility')


class SignupForm(forms.Form):
    email = forms.EmailField(label="Email", required=True, widget=forms.EmailInput(attrs={'class': 'form-control text-center', 'placeholder': 'Email Address'}))
    name = forms.CharField(label="Your name", widget=forms.TextInput(attrs={'class': 'form-control text-center', 'placeholder': 'Your Name'}))
    promised = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={'class': 'form-check'}))
    role = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs=BOOTSTRAP_ATTRS))
    next = forms.CharField(required=False, widget=forms.HiddenInput())
    notes = forms.CharField(required=False, initial='')
    commit = forms.IntegerField(required=False, initial=0, widget=forms.NumberInput(attrs={'class': 'form-control text-center', 'min': 0, 'max': 1000}))

    def __init__(self, *args, **kwargs):
        self.action = kwargs.pop('action')
        super().__init__(*args, **kwargs)
        self.fields['role'].queryset = qset = ActionRole.objects.filter(name__in=self.action.available_role_choices)
        if qset:
            self.fields['role'].required = True


class TalkProposalForm(forms.Form):
    location = forms.CharField(widget=forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'Your location'}))
    name = forms.CharField(label="Your name", required=True, widget=forms.TextInput(attrs={'class': 'form-control text-center', 'placeholder': 'Your Name'}))
    email = forms.EmailField(label="Email", required=True, widget=forms.EmailInput(attrs={'class': 'form-control text-center', 'placeholder': 'Email Address'}))
    phone = PhoneNumberField(label="Phone Number", required=False, widget=forms.TextInput(attrs={'class': 'form-control text-center', 'placeholder': 'Phone Number'}))

# Read all the params or raise exceptions.
def _get_action_request_params(request):
    token = request.GET.get('token', '')
    if token:
        try:
            user_id = signing.Signer().unsign(token)
        except signing.BadSignature:
            raise PermissionDenied()
        else:
            user = get_user_model().objects.get(pk=user_id)
    else:
        user = request.user
    req_date = request.GET.get('month','')
    if req_date:
        current_date = datetime.strptime(req_date, '%Y-%m')
    else:
        current_date = localtime().date().replace(day=1)

    tag_filter = request.GET.get('tag', '')
    page = int(request.GET.get('page', '1'))
    if page < 1:
        raise ValueError
    params = {}
    params['date'] = current_date
    params['tag'] = tag_filter
    params['page'] = page
    return user, params

def _make_date_range(current_date, include_future=True, include_past=7):
    start_date = current_date - timedelta(include_past)
    if not include_future:
        end_date = current_date + timedelta(38)
    else:
        end_date = current_date + timedelta(3650)
    return start_date, end_date

def calendar_view(request, whatever):
    from ics import Calendar, Event
    user, params = _get_action_request_params(request)
    date_range = _make_date_range(params['date'], include_future=True, include_past=30)

    actions = Action.objects.for_user(user).filter(when__date__range=date_range)
    thecal = Calendar()
    thecal.creator = 'XR Mass Events'
    for action in actions:
        evt = Event()
        evt.uid = '{}@{}'.format(action.id, request.get_host())
        evt.name = action.html_title
        evt.description = action.description
        evt.categories = action.tags.names()
        evt.last_modified = action.modified
        evt.url = request.build_absolute_uri(action.get_absolute_url())
        evt.begin = action.when
        evt.duration = timedelta(hours=1)
        # evt.end = action.when + timedelta(hours=1)
        evt.location = action.location
        thecal.events.add(evt)
    response = HttpResponse(thecal, content_type='text/calendar')
    return response

def _add_action_tag_color(action):
    event_colors = {
        'talk': 'xr-bg-pink',
        'action': 'xr-bg-green',
        'ally': 'xr-bg-light-green',
        'meeting': 'xr-bg-lemon',
        'orientation': 'xr-bg-purple',
        'art': 'xr-bg-warm-yellow',
        'nvda': 'xr-bg-light-blue',
        'regen': 'xr-warm-yellow xr-bg-dark-blue',
    }

    # Find first matching color from tags to color mapping.
    colors = (event_colors.get(tag, None) for tag in action.tags.names())
    action.bg_color = next((c for c in colors if c), None)

    return action

@cache_page(1200)
@csrf_protect
def list_actions(request):
    can_add = request.user.has_perm('actions.add_action')
    if request.method == 'POST' and can_add:
        form = ActionForm(request.POST)
        if form.is_valid():
            action = form.save()
            return redirect(action.get_absolute_url())
        else:
            print(form.errors)
            return HttpResponseBadRequest

    # Attempt to read user and any url params:
    try:
        user, params = _get_action_request_params(request)
    except ValueError:
        return HttpResponseBadRequest()

    # Get all the actions for current user.
    actions = Action.objects.for_user(user)

    # Filter by tag
    tag_filter = params['tag']
    if tag_filter:
        actions = actions.filter(tags__name=tag_filter)

    # Generate view for upcoming actions.
    future_actions = actions.filter(when__gte=now().date())

    # Handle pagination range.
    num_pages = math.ceil(future_actions.count() / 10)

    # For pretty urls, page is 1 based.
    start_range = (params['page'] - 1) * 10
    future_actions = future_actions[start_range:start_range+10]

    # Generate view for requested month's actions.
    current_date = params['date']
    date_range = _make_date_range(current_date, include_future=False)
    current_actions = actions.filter(when__date__range=date_range)

    # Generate a mapping from day to list of actions for that day.
    actions_by_day = defaultdict(list)
    for action in current_actions:
        # Convert day to local day so actions land in the right day for current view.
        day = action.when.astimezone(tz.tzlocal()).date()
        actions_by_day[day].append(action)

    # Build a multi-level mapping of the current month in the form of:
    #   month[week[day]] where each day has a list of events.
    cal_days = list(calendar.Calendar(firstweekday=6).itermonthdates(current_date.year, current_date.month))
    month_actions = []
    week_actions = []
    today_tz = localtime().date()

    for daynum, mdate in enumerate(cal_days, 1):
        
        day_actions = list(map(_add_action_tag_color, actions_by_day[mdate]))

        day_data = {
            'day': mdate,
            'is_today': mdate == today_tz,
            'actions': day_actions,
            'is_cur_month': mdate.month == current_date.month
        }
        week_actions.append(day_data)
        if daynum % 7 == 0:
            month_actions.append(week_actions)
            week_actions = []
    if week_actions:
        month_actions.append(week_actions)

    ctx = {
        'future_actions' : future_actions,
        'month_actions': month_actions,
        'current_tag': tag_filter,
        'current_date': current_date,
        'next_month': current_date + timedelta(days=31),
        'last_month': current_date + timedelta(days=-1),
        'can_add': can_add,
        'cur_page': params['page'],
        'pages': range(1, num_pages+1),
    }

    if can_add:
        ctx['form'] = ActionForm()
    
    calendar_link = 'webcal://{}/action/ical/XR%20Mass%20Events'.format(request.get_host())
    link_pars = {}
    if request.user.is_authenticated:
        link_pars['token'] = signing.Signer().sign(request.user.id)
    if tag_filter:
        link_pars['tag'] = tag_filter
    ctx['calendar_link'] = calendar_link + '?' + urlencode(link_pars)
    resp = render(request, 'list_actions.html', ctx)
    resp['Vary'] = 'Cookie'

    if request.user.is_authenticated:
        resp['Cache-Control'] = 'private'
    if actions:
        resp['Last-Modified'] = http_date(actions.last().when.timestamp())
    return resp



@cache_page(1200)
def show_action(request, slug):
    action = get_object_or_404(Action, slug=slug)
    ctx = {'action': action}
    if request.user.is_authenticated:
        ctx['attendees'] = Attendee.objects.filter(action=action).select_related('contact').order_by('-mutual_commitment', '-promised', 'pk')
        ctx['promised'] = ctx['attendees'].filter(promised__isnull=False)
        ctx['default_to_email'] = settings.DEFAULT_FROM_EMAIL
    if action.when < now() and action.public:
        # don't allow signups for public actions that already happened
        ctx['already_happened'] = True
        form = None
    elif request.method == 'POST':
        form = SignupForm(request.POST, action=action)
        if form.is_valid():
            data = form.cleaned_data
            commit = abs(data['commit'] or 0)
            atten = action.signup(data['email'],
                data['role'],
                name=data['name'][:100],
                promised=data['promised'],
                commit=commit,
                notes=data['notes'])
            next_url = data['next'] or request.headers.get('referer', '/')
            messages.success(request, "Thank you for signing up for {}!".format(action.html_title))
            if commit:
                messages.info(request, "We will notify you once at least %d others commit" % commit)
            set_last_contact(request, atten.contact)
            return redirect(next_url)
    else:
        contact = get_contact(email=request.user.email) if request.user.is_authenticated else get_last_contact(request)
        initial = {}
        if contact:
            initial['email'] = contact.email
            initial['name'] = str(contact)
        form = SignupForm(action=action, initial=initial)
    ctx['form'] = form
    ctx['has_roles'] = list(action.available_role_choices)
    ctx['photos'] = list(action.photos.all())
    resp = render(request, 'action.html', ctx)
    resp['Vary'] = 'Cookie'
    resp['Last-Modified'] = http_date(action.modified.timestamp())
    if request.user.is_authenticated:
        resp['Cache-Control'] = 'private'
    return resp


@never_cache
def show_attendees(request, action_slug):
    action = get_object_or_404(Action, slug=action_slug)
    out_fmt = request.GET.get('fmt', 'json')
    attendees = Attendee.objects.filter(action=action).select_related('contact').order_by('contact__last_name')
    num = attendees.count()
    if num > 10:
        half = int(num / 2)
    else:
        half = None
    if out_fmt == 'html':
        resp = HttpResponse('not allowed')

        # ctx = {'attendees': attendees, 'half': half, 'can_change': request.user.is_staff, 'slug': action_slug}
        # resp = render(request, 'attendees.html', ctx)
    elif out_fmt == 'csv' and request.user.has_perm('actions.view_attendee'):
        attendees = attendees.order_by('created')
        resp = HttpResponse()
        resp['Content-Type'] = 'text/csv'
        csv_writer = csv.writer(resp)
        header = ('Email', 'First Name', 'Last Name', 'Phone', 'Promised', 'Created')
        csv_writer.writerow(header)
        for attendee in attendees:
            csv_writer.writerow((attendee.contact.email, attendee.contact.first_name, attendee.contact.last_name, attendee.contact.phone, attendee.promised, attendee.created.isoformat()))
    return resp


@login_required
def send_notifications(request, action_slug):
    action = get_object_or_404(Action, slug=action_slug)
    if request.method == 'POST':
        threshold = int(request.POST['threshold'])
        action_url = request.build_absolute_uri(reverse('actions:action', kwargs={'slug': action_slug}))
        num = notify_commitments(action, threshold, action_url)
        if num:
            messages.success(request, 'Notified %d attendees of their commitment!' % num)
    return redirect(action.get_absolute_url())


def propose_talk(request):
    ctx = {}
    if request.method == 'POST':
        form = TalkProposalForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            prop = TalkProposal.objects.propose(
                strip_tags(data['location']),
                data['email'],
                phone=data['phone'],
                name=data['name'])
            ctx['created'] = prop
            messages.success(request, 'Thank you, {}!'.format(prop.requestor))
            messages.info(request, 'Somebody from Extinction Rebellion will contact you soon to arrange a talk at {}'.format(prop.location))
            set_last_contact(request, prop.requestor)
            return redirect(reverse('extinctionr.actions:talk-proposal'))
    else:
        contact = get_last_contact(request)
        initial = {}
        if contact:
            initial['email'] = contact.email
            initial['name'] = str(contact)
            initial['phone'] = contact.phone
        form = TalkProposalForm(initial=initial)
    ctx['form'] = form
    return render(request, 'talkproposal.html', ctx)


@login_required
def mark_promised(request, action_slug):
    if request.user.has_perm('action.change_attendee'):
        attendee = get_object_or_404(Attendee, pk=request.POST['id'], action__slug=action_slug)
        if not attendee.promised:
            attendee.promised = now()
            attendee.save()
    return JsonResponse({'status': 'ok'})


@login_required
@never_cache
def list_proposals(request):
    ctx = {
        'talks': TalkProposal.objects.select_related('requestor').order_by('-responded', 'created')
    }
    template = 'list_talks'
    if request.GET.get('format', 'html') == 'csv':
        template += '.csv'
        content_type = 'text/csv'
        content_disposition = 'attachment; filename="talks.csv"'
    else:
        template += '.html'
        content_type = 'text/html'
        content_disposition = None
    response = render(request, template, ctx)
    response['content-type'] = content_type
    if content_disposition:
        response['content-disposition'] = content_disposition
    return response



@login_required
@never_cache
def talk_respond(request, talk_id):
    talk = get_object_or_404(TalkProposal, pk=talk_id)
    if request.method == 'POST' and not talk.responded:
        talk.responded = now()
        talk.responder = request.user
        talk.save()
    return JsonResponse({'id': talk.id})


@login_required
@never_cache
def convert_proposal_to_action(request, talk_id):
    talk = get_object_or_404(TalkProposal, pk=talk_id)
    if request.method == 'POST' and talk.responded:
        act = Action()
        act.name = "XR Talk at {}".format(talk.location.strip())
        act.when = now() + timedelta(days=7)
        act.public = False
        act.description = '''Heading to extinction (and what to do about it)

This talk will be at {}
'''.format(talk.location)
        act.slug = 'xr-talk-%d' % talk.id
        try:
            act.save()
        except IntegrityError:
            act = Action.objects.get(slug=act.slug)
        url = '/admin/actions/action/%d/change/' % act.id
        return JsonResponse({'next': url})

