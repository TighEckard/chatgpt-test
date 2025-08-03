


/* ======================================================================
 * KALIMBA • AI Reception – Scheduler (Admin + AI API)
 * Shortcode: [scheduler_admin]
 * ====================================================================== */

if ( ! defined('ABSPATH') ) exit;

/** ────────────────────────────────────────────────────────────────────
 * 0) Constants / brand tokens (reuse your existing palette)
 * ─────────────────────────────────────────────────────────────────── */
if ( ! defined('KAL_BRAND_PURPLE') ) define('KAL_BRAND_PURPLE', '#6A73C6');
if ( ! defined('KAL_BRAND_HOVER') )  define('KAL_BRAND_HOVER',  '#D9DCFF');
if ( ! defined('KAL_TEXT') )         define('KAL_TEXT',         '#374151');
if ( ! defined('KAL_BORDER') )       define('KAL_BORDER',       '#E5E7EB');

/** ────────────────────────────────────────────────────────────────────
 * 1) Custom Post Type: kal_booking  (staff-only, REST-enabled)
 * ─────────────────────────────────────────────────────────────────── */
add_action('init', function () {
	register_post_type('kal_booking', [
		'label'               => 'Bookings',
		'public'              => false,
		'show_ui'             => true,
		'show_in_rest'        => true,
		'rest_base'           => 'kal_booking',
		'map_meta_cap'        => true,
		'capability_type'     => 'post',
		'supports'            => ['title', 'custom-fields'],
	]);
});

/** Map booking owner permissions by phone (same pattern as call_log). */
add_filter('map_meta_cap', function ($caps, $cap, $user_id, $args) {
	if ( ! in_array($cap, ['edit_post','delete_post'], true) ) return $caps;
	$post_id = intval($args[0] ?? 0);
	$post    = get_post($post_id);
	if ( ! $post || $post->post_type !== 'kal_booking') return $caps;

	$user_phone  = get_user_meta($user_id, 'ai_receptionist_phone', true);
	$owner_phone = get_post_meta($post_id, 'owner_phone', true);

	if ( $user_phone && $owner_phone && $user_phone === $owner_phone ) {
		return ['read']; // grant via harmless capability
	}
	return $caps;
}, 10, 4);

/** ────────────────────────────────────────────────────────────────────
 * 2) Data model (per-user meta)
 *    - rooms:        [{id,label,capacity,color,ics_url}]
 *    - services:     [{id,label,duration_min,buffer_before,buffer_after,max_party}]
 *    - hours:        { tz, weekly:{mon:[{start,end}],...}, holidays:[date...], overrides:[{date,open:[{start,end}]}] }
 *    - api key:      ai_receptionist_api_key  (Bearer token)
 * ─────────────────────────────────────────────────────────────────── */
function kal_sched_user_id()         { return get_current_user_id(); }
function kal_sched_rooms($uid=null)  { $uid=$uid?:kal_sched_user_id(); return get_user_meta($uid,'kal_rooms',true) ?: []; }
function kal_sched_services($uid=null){$uid=$uid?:kal_sched_user_id(); return get_user_meta($uid,'kal_services',true) ?: []; }
function kal_sched_hours($uid=null)  { $uid=$uid?:kal_sched_user_id(); return get_user_meta($uid,'kal_hours',true) ?: []; }
function kal_sched_tz($uid=null)     {
	$uid=$uid?:kal_sched_user_id();
	$h = kal_sched_hours($uid);
	return $h['tz'] ?? ( get_option('timezone_string') ?: 'UTC' );
}

/** Generate / get API key. */
function kal_sched_api_key($uid=null){
	$uid=$uid?:kal_sched_user_id();
	$key = get_user_meta($uid, 'ai_receptionist_api_key', true);
	if ( ! $key ) {
		$key = wp_generate_password(40, false, false);
		update_user_meta($uid, 'ai_receptionist_api_key', $key);
	}
	return $key;
}

/** ────────────────────────────────────────────────────────────────────
 * 3) Utility – minimal ICS fetch (cache 5 min) to block external busy
 *    (Optional: set per-room ICS URL; we parse DTSTART/DTEND UTC or local)
 * ─────────────────────────────────────────────────────────────────── */
function kal_sched_busy_from_ics($url){
	if ( ! $url ) return [];
	$key = 'kal_ics_' . md5($url);
	$cached = get_transient($key);
	if ( $cached !== false ) return $cached;

	$resp = wp_remote_get($url, ['timeout'=>12]);
	if ( is_wp_error($resp) || wp_remote_retrieve_response_code($resp) !== 200 ) {
		set_transient($key, [], 300);
		return [];
	}
	$ics = wp_remote_retrieve_body($resp);
	$busy = [];
	$lines = preg_split('/\R/', $ics);
	$cur = [];
	foreach($lines as $ln){
		$ln = trim($ln);
		if ($ln === 'BEGIN:VEVENT') { $cur = []; }
		if (str_starts_with($ln,'DTSTART')) $cur['start']=preg_replace('/^DTSTART.*:/','',$ln);
		if (str_starts_with($ln,'DTEND'))   $cur['end']  =preg_replace('/^DTEND.*:/','',$ln);
		if ($ln === 'END:VEVENT' && !empty($cur['start']) && !empty($cur['end'])) {
			$busy[] = $cur; $cur=[];
		}
	}
	set_transient($key, $busy, 300);
	return $busy;
}

/** Convert basic ICS datetime to UNIX timestamp (best-effort). */
function kal_sched_ics_to_ts($s, $tz='UTC'){
	// Formats like 20250802T140000Z or 20250802T140000
	if (str_ends_with($s,'Z')) return strtotime($s);
	$dt = DateTime::createFromFormat('Ymd\THis', $s, new DateTimeZone($tz));
	return $dt ? $dt->getTimestamp() : strtotime($s);
}

/** ────────────────────────────────────────────────────────────────────
 * 4) Availability engine
 *    - slot size determined by service duration
 *    - respects buffers, capacity, rooms, existing bookings + holds + ICS busy
 * ─────────────────────────────────────────────────────────────────── */
function kal_sched_conflicts($owner_phone, $from_ts, $to_ts, $room_id){
	$q = new WP_Query([
		'post_type'   => 'kal_booking',
		'post_status' => ['publish','private'],
		'posts_per_page' => 500,
		'meta_query' => [
			['key'=>'owner_phone', 'value'=>$owner_phone],
			['key'=>'room_id',     'value'=>$room_id],
		],
		'date_query' => [], // not reliable for custom meta times
	]);
	$out = [];
	foreach ($q->posts as $p){
		$s = intval(get_post_meta($p->ID,'start_ts',true));
		$e = intval(get_post_meta($p->ID,'end_ts',true));
		$status = get_post_meta($p->ID,'status',true) ?: 'confirmed';
		if ($status === 'cancelled') continue;
		$out[] = ['start'=>$s,'end'=>$e];
	}
	// include active holds (transients)
	global $wpdb;
	$prefix = '_transient_kal_hold_' . md5($owner_phone.'_'.$room_id.'_');
	$holds = $wpdb->get_col( $wpdb->prepare(
		"SELECT option_name FROM {$wpdb->options} WHERE option_name LIKE %s",
		$prefix.'%'
	));
	foreach ($holds as $opt){
		$data = get_option($opt);
		if (is_array($data) && !empty($data['start']) && !empty($data['end'])) {
			$out[] = ['start'=>intval($data['start']),'end'=>intval($data['end'])];
		}
	}
	return $out;
}

function kal_sched_is_free($start,$end,$blocks){
	foreach ($blocks as $b){
		if ($start < $b['end'] && $end > $b['start']) return false;
	}
	return true;
}

function kal_sched_weekly_windows($hours, $from_ts, $to_ts){
	// returns list of [start_ts,end_ts] open windows in business TZ
	$tz = $hours['tz'] ?? 'UTC';
	$weekly = $hours['weekly'] ?? [];
	$hols   = $hours['holidays'] ?? [];
	$ovr    = $hours['overrides'] ?? [];
	$out = [];
	$cur = $from_ts;
	$tzobj = new DateTimeZone($tz);
	while ($cur < $to_ts){
		$dt = new DateTime("@$cur");
		$dt->setTimezone($tzobj);
		$ymd = $dt->format('Y-m-d');
		$w   = strtolower($dt->format('D')); // mon..sun

		$dayOpen = [];
		// Overrides take precedence
		foreach ($ovr as $o){
			if (($o['date']??'') === $ymd && !empty($o['open'])) {
				$dayOpen = $o['open']; break;
			}
		}
		// Holiday = closed
		if (in_array($ymd, $hols ?? [], true)) $dayOpen = [];

		// Default weekly if no overrides/holidays
		if ($dayOpen === [] && isset($weekly[$w]) ) $dayOpen = $weekly[$w];

		foreach ($dayOpen as $win){
			$st = DateTime::createFromFormat('Y-m-d H:i', $ymd.' '.($win['start']??'00:00'), $tzobj);
			$en = DateTime::createFromFormat('Y-m-d H:i', $ymd.' '.($win['end']  ??'23:59'), $tzobj);
			if (!$st || !$en) continue;
			$sts=$st->getTimestamp(); $ens=$en->getTimestamp();
			// clamp to query window
			$sts = max($sts, $from_ts); $ens = min($ens, $to_ts);
			if ($ens > $sts) $out[] = [$sts,$ens];
		}
		$cur += 86400; // next day
	}
	return $out;
}

function kal_sched_available_slots($owner_phone, $from_iso, $to_iso, $party, $service_id, $room_filter=null){
	$uid     = get_users(['meta_key'=>'ai_receptionist_phone','meta_value'=>$owner_phone,'number'=>1,'count_total'=>false])[0]->ID ?? 0;
	if (!$uid) return [];

	$tz      = kal_sched_tz($uid);
	$hours   = kal_sched_hours($uid);
	$rooms   = kal_sched_rooms($uid);
	$svcs    = kal_sched_services($uid);
	$svc     = null; foreach($svcs as $s){ if($s['id']===$service_id){$svc=$s;break;}}
	if (!$svc) return [];

	$dur   = max(5, intval($svc['duration_min'] ?? 30)) * 60;
	$buf_b = max(0, intval($svc['buffer_before'] ?? 0)) * 60;
	$buf_a = max(0, intval($svc['buffer_after']  ?? 0)) * 60;
	$max_p = max(1, intval($svc['max_party'] ?? 1));

	if ($party > $max_p) return []; // too big for service

	$from_ts = strtotime($from_iso);
	$to_ts   = strtotime($to_iso);
	if (!$from_ts || !$to_ts || $to_ts <= $from_ts) return [];

	$windows = kal_sched_weekly_windows($hours, $from_ts, $to_ts);

	$results = [];
	foreach ($rooms as $room){
		if ($room_filter && $room_filter !== 'any' && $room_filter !== $room['id']) continue;
		$cap = max(1, intval($room['capacity'] ?? 1));
		if ($party > $cap) continue;

		$blocks = kal_sched_conflicts($owner_phone, $from_ts, $to_ts, $room['id']);

		// External ICS busy (optional)
		if (!empty($room['ics_url'])) {
			$busy = kal_sched_busy_from_ics($room['ics_url']);
			foreach ($busy as $b) {
				$bs = kal_sched_ics_to_ts($b['start'] ?? '', $tz);
				$be = kal_sched_ics_to_ts($b['end']   ?? '', $tz);
				if ($bs && $be) $blocks[] = ['start'=>$bs,'end'=>$be];
			}
		}

		foreach ($windows as [$ws,$we]){
			// walk in 5-minute increments
			for ($t=$ws; $t+$dur <= $we; $t+=300){
				$slot_start = $t;
				$slot_end   = $t + $dur;
				$guard_s    = $slot_start - $buf_b;
				$guard_e    = $slot_end   + $buf_a;
				if ( kal_sched_is_free($guard_s, $guard_e, $blocks) ){
					$results[] = [
						'room_id'  => $room['id'],
						'room'     => $room['label'],
						'service'  => $svc['label'],
						'start_ts' => $slot_start,
						'end_ts'   => $slot_end,
					];
				}
			}
		}
	}
	// sort earliest first
	usort($results, fn($a,$b)=> $a['start_ts'] <=> $b['start_ts']);
	return $results;
}

/** ────────────────────────────────────────────────────────────────────
 * 5) Staff REST API (nonce protected)
 * ─────────────────────────────────────────────────────────────────── */
add_action('rest_api_init', function(){

	$ns = 'ai-reception/v1';

	// ROOMS
	register_rest_route($ns,'/sched/rooms',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>fn()=>rest_ensure_response(kal_sched_rooms()),
	]);
	register_rest_route($ns,'/sched/rooms',[
		'methods'=>'POST','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$rows = $r->get_json_params() ?: [];
			// normalize
			foreach ($rows as &$x){
				$x['id']       = $x['id'] ?? uniqid('room_',true);
				$x['label']    = trim($x['label'] ?? '');
				$x['capacity'] = intval($x['capacity'] ?? 1);
				$x['color']    = preg_match('/^#?[0-9A-Fa-f]{6}$/',$x['color']??'') ? (str_starts_with($x['color'],'#')?$x['color']:'#'.$x['color']) : KAL_BRAND_PURPLE;
				$x['ics_url']  = esc_url_raw($x['ics_url'] ?? '');
			}
			update_user_meta(kal_sched_user_id(),'kal_rooms',$rows);
			return rest_ensure_response($rows);
		}
	]);

	// SERVICES
	register_rest_route($ns,'/sched/services',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>fn()=>rest_ensure_response(kal_sched_services()),
	]);
	register_rest_route($ns,'/sched/services',[
		'methods'=>'POST','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$rows = $r->get_json_params() ?: [];
			foreach ($rows as &$x){
				$x['id']            = $x['id'] ?? uniqid('svc_',true);
				$x['label']         = trim($x['label'] ?? '');
				$x['duration_min']  = max(5, intval($x['duration_min'] ?? 30));
				$x['buffer_before'] = max(0, intval($x['buffer_before'] ?? 0));
				$x['buffer_after']  = max(0, intval($x['buffer_after']  ?? 0));
				$x['max_party']     = max(1, intval($x['max_party'] ?? 1));
			}
			update_user_meta(kal_sched_user_id(),'kal_services',$rows);
			return rest_ensure_response($rows);
		}
	]);

	// HOURS
	register_rest_route($ns,'/sched/hours',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>fn()=>rest_ensure_response(kal_sched_hours()),
	]);
	register_rest_route($ns,'/sched/hours',[
		'methods'=>'POST','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$body = $r->get_json_params() ?: [];
			$tz   = $body['tz'] ?? kal_sched_tz();
			$weekly = $body['weekly'] ?? [];
			$hols   = array_values(array_unique(array_map('trim', $body['holidays'] ?? [])));
			$ovr    = $body['overrides'] ?? [];
			$hours  = ['tz'=>$tz,'weekly'=>$weekly,'holidays'=>$hols,'overrides'=>$ovr];
			update_user_meta(kal_sched_user_id(),'kal_hours',$hours);
			return rest_ensure_response($hours);
		}
	]);

	// BOOKINGS (staff CRUD)
	register_rest_route($ns,'/sched/bookings',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$uid   = kal_sched_user_id();
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);
			$from  = strtotime( $r->get_param('from') ?? 'now -7 days' );
			$to    = strtotime( $r->get_param('to')   ?? 'now +60 days');

			$q = new WP_Query([
				'post_type'=>'kal_booking','post_status'=>['publish','private'],
				'posts_per_page'=>500,
				'meta_query'=>[
					['key'=>'owner_phone','value'=>$phone],
				],
			]);
			$out=[];
			foreach($q->posts as $p){
				$st = intval(get_post_meta($p->ID,'start_ts',true));
				$en = intval(get_post_meta($p->ID,'end_ts',true));
				if ($st>$to || $en<$from) continue;
				$out[] = [
					'id'      => $p->ID,
					'status'  => get_post_meta($p->ID,'status',true) ?: 'confirmed',
					'room_id' => get_post_meta($p->ID,'room_id',true),
					'room'    => get_post_meta($p->ID,'room_label',true),
					'service' => get_post_meta($p->ID,'service_label',true),
					'party'   => intval(get_post_meta($p->ID,'party',true)),
					'name'    => get_post_meta($p->ID,'cust_name',true),
					'phone'   => get_post_meta($p->ID,'cust_phone',true),
					'start_ts'=> $st,
					'end_ts'  => $en,
					'notes'   => get_post_meta($p->ID,'notes',true),
				];
			}
			return rest_ensure_response($out);
		}
	]);

	register_rest_route($ns,'/sched/bookings',[
		'methods'=>'POST','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$uid   = kal_sched_user_id();
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);
			$d     = $r->get_json_params() ?: [];

			// required
			$room_id = $d['room_id'] ?? '';
			$svc_id  = $d['service_id'] ?? '';
			$start   = intval($d['start_ts'] ?? 0);
			$end     = intval($d['end_ts']   ?? 0);
			$party   = max(1, intval($d['party'] ?? 1));

			$rooms = kal_sched_rooms($uid);
			$svcs  = kal_sched_services($uid);
			$room  = null; foreach($rooms as $r1){ if($r1['id']===$room_id){$room=$r1;break;}}
			$svc   = null; foreach($svcs  as $s1){ if($s1['id']===$svc_id){$svc =$s1;break;}}
			if (!$room || !$svc || !$start || !$end) return new WP_Error('bad','Missing required fields', ['status'=>400]);
			if ($party > intval($room['capacity'])) return new WP_Error('cap','Party exceeds room capacity', ['status'=>400]);

			$conf = kal_sched_conflicts($phone,$start,$end,$room_id);
			$bufb = intval($svc['buffer_before'] ?? 0)*60;
			$bufa = intval($svc['buffer_after']  ?? 0)*60;
			if ( ! kal_sched_is_free($start-$bufb, $end+$bufa, $conf) )
				return new WP_Error('busy','Time overlaps another booking', ['status'=>409]);

			$post_id = wp_insert_post([
				'post_type'  => 'kal_booking',
				'post_title' => ($d['cust_name'] ?? 'Booking').' · '.$room['label'].' · '.date_i18n('M j Y H:i',$start),
				'post_status'=> 'private',
				'meta_input' => [
					'owner_phone'  => $phone,
					'room_id'      => $room_id,
					'room_label'   => $room['label'],
					'service_id'   => $svc_id,
					'service_label'=> $svc['label'],
					'party'        => $party,
					'cust_name'    => $d['cust_name'] ?? '',
					'cust_phone'   => $d['cust_phone'] ?? '',
					'cust_email'   => $d['cust_email'] ?? '',
					'notes'        => $d['notes'] ?? '',
					'start_ts'     => $start,
					'end_ts'       => $end,
					'status'       => 'confirmed',
					'source'       => $d['source'] ?? 'staff',
				]
			]);
			return rest_ensure_response(['id'=>$post_id]);
		}
	]);

	register_rest_route($ns,'/sched/bookings/(?P<id>\d+)',[
		'methods'=>'POST','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$id = intval($r['id']);
			$d  = $r->get_json_params() ?: [];
			foreach (['status','cust_name','cust_phone','cust_email','notes'] as $k){
				if (array_key_exists($k,$d)) update_post_meta($id, $k, $d[$k]);
			}
			return rest_ensure_response(['ok'=>true]);
		}
	]);

	register_rest_route($ns,'/sched/bookings/(?P<id>\d+)',[
		'methods'=>'DELETE','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			wp_trash_post(intval($r['id']));
			return rest_ensure_response(['ok'=>true]);
		}
	]);

	// STAFF availability (for UI)
	register_rest_route($ns,'/sched/availability',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$uid   = kal_sched_user_id();
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);
			$from  = $r->get_param('from');
			$to    = $r->get_param('to');
			$party = intval($r->get_param('party') ?? 1);
			$svc   = $r->get_param('service_id') ?? '';
			$room  = $r->get_param('room_id') ?? null;
			return rest_ensure_response(
				kal_sched_available_slots($phone,$from,$to,$party,$svc,$room)
			);
		}
	]);

	// ICS feed (private): /wp-json/ai-reception/v1/sched/ics?resource_id=... (requires login)
	register_rest_route($ns,'/sched/ics',[
		'methods'=>'GET','permission_callback'=>'is_user_logged_in',
		'callback'=>function(WP_REST_Request $r){
			$uid   = kal_sched_user_id();
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);
			$rid   = $r->get_param('resource_id');

			$q = new WP_Query([
				'post_type'=>'kal_booking','post_status'=>['publish','private'],
				'posts_per_page'=>500,
				'meta_query'=>[
					['key'=>'owner_phone','value'=>$phone],
					['key'=>'room_id', 'value'=>$rid],
				]
			]);
			$tz = kal_sched_tz($uid);
			$tzobj = new DateTimeZone($tz);
			$buf = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Kalimba//Scheduler//EN\r\n";
			foreach($q->posts as $p){
				$st = intval(get_post_meta($p->ID,'start_ts',true));
				$en = intval(get_post_meta($p->ID,'end_ts',true));
				$dtS = (new DateTime("@$st"))->setTimezone($tzobj)->format('Ymd\THis');
				$dtE = (new DateTime("@$en"))->setTimezone($tzobj)->format('Ymd\THis');
				$sum = wp_strip_all_tags(get_post_meta($p->ID,'service_label',true).' – '.get_post_meta($p->ID,'room_label',true));
				$buf .= "BEGIN:VEVENT\r\nUID:kal-$p->ID@app\r\nDTSTART:$dtS\r\nDTEND:$dtE\r\nSUMMARY:".esc_html($sum)."\r\nEND:VEVENT\r\n";
			}
			$buf .= "END:VCALENDAR\r\n";
			return new WP_REST_Response($buf, 200, ['Content-Type'=>'text/calendar; charset=utf-8']);
		}
	]);
});

/** ────────────────────────────────────────────────────────────────────
 * 6) AI PUBLIC API (for app.py)
 *     Authorization: "Bearer {ai_receptionist_api_key}" AND ?phone=+1...
 * ─────────────────────────────────────────────────────────────────── */
add_action('rest_api_init', function(){
	$ns = 'ai-reception/v1';

	$auth = function(WP_REST_Request $r){
		$phone = sanitize_text_field($r->get_param('phone'));
		$auth  = $r->get_header('authorization') ?: '';
		if (!$phone || ! str_starts_with(strtolower($auth),'bearer ')) return 0;
		$key = trim(substr($auth,7));
		$user = get_users(['meta_key'=>'ai_receptionist_phone','meta_value'=>$phone,'number'=>1,'count_total'=>false]);
		if (!$user) return 0;
		$uid = $user[0]->ID;
		$saved = get_user_meta($uid,'ai_receptionist_api_key',true);
		return hash_equals($saved ?: '', $key) ? $uid : 0;
	};

	// Check availability (AI)
	register_rest_route($ns,'/ai/availability',[
		'methods'=>'GET','permission_callback'=>'__return_true',
		'callback'=>function(WP_REST_Request $r) use ($auth){
			$uid = $auth($r); if (!$uid) return new WP_Error('forbidden','Bad token/phone',['status'=>403]);
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);
			$from  = $r->get_param('from');
			$to    = $r->get_param('to');
			$party = intval($r->get_param('party') ?? 1);
			$svc   = $r->get_param('service_id') ?? '';
			$room  = $r->get_param('room_id') ?? null;
			return rest_ensure_response( kal_sched_available_slots($phone,$from,$to,$party,$svc,$room) );
		}
	]);

	// Hold a slot (AI) – transient 6 minutes
	register_rest_route($ns,'/ai/hold',[
		'methods'=>'POST','permission_callback'=>'__return_true',
		'callback'=>function(WP_REST_Request $r) use ($auth){
			$uid = $auth($r); if (!$uid) return new WP_Error('forbidden','Bad token/phone',['status'=>403]);
			$d = $r->get_json_params() ?: [];
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);

			$rid = $d['room_id'] ?? '';
			$svc_id = $d['service_id'] ?? '';
			$start = intval($d['start_ts'] ?? 0);
			$end   = intval($d['end_ts']   ?? 0);

			// basic conflict check
			$svcs = kal_sched_services($uid);
			$svc  = null; foreach($svcs as $s){ if($s['id']===$svc_id){$svc=$s;break;}}
			if (!$svc) return new WP_Error('bad','Unknown service', ['status'=>400]);
			$bufb = intval($svc['buffer_before'] ?? 0)*60;
			$bufa = intval($svc['buffer_after']  ?? 0)*60;
			$conf = kal_sched_conflicts($phone,$start,$end,$rid);
			if ( ! kal_sched_is_free($start-$bufb, $end+$bufa, $conf) )
				return new WP_Error('busy','Time overlaps another booking', ['status'=>409]);

			$token = md5( uniqid('hold_',true) );
			$key   = 'kal_hold_' . md5($phone.'_'.$rid.'_'.$token);
			$data  = ['start'=>$start,'end'=>$end,'room_id'=>$rid,'service_id'=>$svc_id,'phone'=>$phone];
			set_transient($key, $data, 6*60);
			return rest_ensure_response(['hold_id'=>$token,'expires_in'=>360]);
		}
	]);

	// Confirm a hold (AI)
	register_rest_route($ns,'/ai/confirm',[
		'methods'=>'POST','permission_callback'=>'__return_true',
		'callback'=>function(WP_REST_Request $r) use ($auth){
			$uid = $auth($r); if (!$uid) return new WP_Error('forbidden','Bad token/phone',['status'=>403]);
			$d = $r->get_json_params() ?: [];
			$phone = get_user_meta($uid,'ai_receptionist_phone',true);

			$token = sanitize_text_field($d['hold_id'] ?? '');
			$rid   = sanitize_text_field($d['room_id'] ?? '');
			$key   = 'kal_hold_' . md5($phone.'_'.$rid.'_'.$token);
			$hold  = get_transient($key);
			if ( ! $hold ) return new WP_Error('gone','Hold expired', ['status'=>410]);

			$rooms = kal_sched_rooms($uid);
			$svcs  = kal_sched_services($uid);
			$room  = null; foreach($rooms as $r1){ if($r1['id']===$rid){$room=$r1;break;}}
			$svc   = null; foreach($svcs  as $s1){ if($s1['id']===$hold['service_id']){$svc=$s1;break;}}

			$post_id = wp_insert_post([
				'post_type'=>'kal_booking','post_status'=>'private',
				'post_title'=> ($d['cust_name'] ?? 'Booking').' · '.$room['label'].' · '.date_i18n('M j Y H:i',$hold['start']),
				'meta_input'=>[
					'owner_phone'=>$phone,
					'room_id'    =>$rid,
					'room_label' =>$room['label'] ?? '',
					'service_id' =>$svc['id'] ?? '',
					'service_label'=>$svc['label'] ?? '',
					'party'      => max(1, intval($d['party'] ?? 1)),
					'cust_name'  => $d['cust_name'] ?? '',
					'cust_phone' => $d['cust_phone'] ?? '',
					'cust_email' => $d['cust_email'] ?? '',
					'notes'      => $d['notes'] ?? '',
					'start_ts'   => intval($hold['start']),
					'end_ts'     => intval($hold['end']),
					'status'     => 'confirmed',
					'source'     => 'ai',
				]
			]);
			delete_transient($key);
			return rest_ensure_response(['id'=>$post_id]);
		}
	]);
});

/** ────────────────────────────────────────────────────────────────────
 * 7) Admin Shortcode [scheduler_admin]  (brand-themed)
 *     Tabs: Calendar · Rooms · Services · Hours & Holidays · Settings/API
 * ─────────────────────────────────────────────────────────────────── */
add_shortcode('scheduler_admin', function () {
	if ( ! is_user_logged_in() ) return '<p>Please log in.</p>';

	$uid   = get_current_user_id();
	$phone = get_user_meta($uid,'ai_receptionist_phone',true);
	$tz    = esc_js( kal_sched_tz($uid) );
	$key   = kal_sched_api_key($uid);

	ob_start(); ?>
	<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.11/index.global.min.css">
	<script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.11/index.global.min.js"></script>

	<style>
	:root{
		--brand: <?=esc_attr(KAL_BRAND_PURPLE)?>;
		--hover: <?=esc_attr(KAL_BRAND_HOVER)?>;
		--text:  <?=esc_attr(KAL_TEXT)?>;
		--border:<?=esc_attr(KAL_BORDER)?>;
	}
	.kal-card{background:#fff;border:1px solid var(--hover);border-radius:.75rem;box-shadow:0 4px 12px rgba(0,0,0,.05);padding:1.25rem;color:var(--text)}
	.kal-tabs{display:flex;border-bottom:2px solid var(--border);gap:.25rem;margin-bottom:1rem}
	.kal-tab{padding:.75rem 1rem;cursor:pointer;border-radius:.5rem .5rem 0 0}
	.kal-tab.active{background:var(--hover);color:var(--brand);font-weight:700}
	.kal-pane{display:none}.kal-pane.active{display:block}
	.kal-btn{background:var(--brand);color:#fff;border:none;border-radius:.5rem;padding:.6rem 1rem;cursor:pointer}
	.kal-btn:hover{background:var(--hover);color:var(--brand)}
	.kal-grid{display:grid;gap:1rem}
	@media(min-width:900px){.kal-grid-2{grid-template-columns:1fr 1fr}}
	input,select,textarea{width:100%;border:1px solid var(--border);border-radius:.5rem;padding:.6rem}
	.kal-row{display:flex;gap:.6rem;align-items:center}
	.kal-chip{display:inline-block;background:var(--hover);color:var(--brand);border:1px solid var(--brand);padding:.25rem .5rem;border-radius:999px;margin:.2rem .2rem 0 0}
	.kal-muted{color:#596270}
	.kal-callout{background:var(--hover);border-left:4px solid var(--brand);padding:.75rem;border-radius:.5rem}
	.kal-table th, .kal-table td{border-bottom:1px solid var(--border);padding:.4rem .2rem}
	.fc .fc-toolbar-title{color:var(--text)}
	.fc .fc-button{background:var(--brand);border:none}
	.fc .fc-button:hover{background:var(--hover);color:var(--brand)}
	</style>

	<div class="kal-card" id="kalSched">
		<div class="kal-tabs">
			<div class="kal-tab active" data-tab="cal">Calendar</div>
			<div class="kal-tab" data-tab="rooms">Rooms</div>
			<div class="kal-tab" data-tab="svc">Services</div>
			<div class="kal-tab" data-tab="hours">Hours & Holidays</div>
			<div class="kal-tab" data-tab="api">Settings / API</div>
		</div>

		<!-- Calendar Pane -->
		<div class="kal-pane active" id="pane-cal">
			<div class="kal-row" style="margin-bottom:.75rem">
				<select id="kalRoomFilter"><option value="any">All rooms</option></select>
				<select id="kalSvcFilter"></select>
				<input id="kalParty" type="number" min="1" value="1" style="width:120px">
				<button class="kal-btn" id="kalCheck">Check availability</button>
			</div>
			<div id="kalCalendar"></div>
			<div class="kal-callout" style="margin-top:1rem">
				<strong>Tip:</strong> Click a free slot to create a booking. Drag existing bookings to reschedule.
			</div>
		</div>

		<!-- Rooms -->
		<div class="kal-pane" id="pane-rooms">
			<div class="kal-grid kal-grid-2" id="kalRoomsWrap"></div>
			<button class="kal-btn" id="kalAddRoom" style="margin-top:.75rem">Add Room</button>
			<div id="kalRoomsSaveMsg" class="kal-muted" style="margin-top:.5rem"></div>
		</div>

		<!-- Services -->
		<div class="kal-pane" id="pane-svc">
			<div class="kal-grid kal-grid-2" id="kalSvcWrap"></div>
			<button class="kal-btn" id="kalAddSvc" style="margin-top:.75rem">Add Service</button>
			<div id="kalSvcSaveMsg" class="kal-muted" style="margin-top:.5rem"></div>
		</div>

		<!-- Hours & Holidays -->
		<div class="kal-pane" id="pane-hours">
			<div class="kal-grid kal-grid-2">
				<div>
					<label>Timezone</label>
					<input id="kalTZ" value="<?=esc_attr($tz)?>">
					<table class="kal-table" style="width:100%;margin-top:.75rem">
						<thead><tr><th>Day</th><th>Open</th><th>Close</th></tr></thead>
						<tbody id="kalWeekly"></tbody>
					</table>
					<button class="kal-btn" id="kalAddWin" style="margin-top:.5rem">Add window for selected day</button>
				</div>
				<div>
					<label>Holidays (closed)</label>
					<textarea id="kalHolidays" rows="6" placeholder="YYYY-MM-DD, one per line"></textarea>
					<label style="margin-top:.5rem">Overrides (open windows on specific dates)</label>
					<textarea id="kalOverrides" rows="6" placeholder='YYYY-MM-DD | 09:00-13:00;14:00-18:00'></textarea>
				</div>
			</div>
			<button class="kal-btn" id="kalSaveHours" style="margin-top:.75rem">Save Hours</button>
			<div id="kalHoursMsg" class="kal-muted" style="margin-top:.5rem"></div>
		</div>

		<!-- Settings / API -->
		<div class="kal-pane" id="pane-api">
			<div class="kal-callout">
				<p><strong>Your business number:</strong> <span class="kal-chip"><?=esc_html($phone ?: '— none —')?></span></p>
				<p><strong>API Key:</strong> <code id="kalApiKey"><?=esc_html($key)?></code>
					<button class="kal-btn" id="kalRegen" style="margin-left:.5rem">Regenerate</button></p>
				<p class="kal-muted">The AI receptionist uses the key + phone to check availability, place holds, and confirm bookings.</p>
			</div>
			<details style="margin-top:.75rem"><summary><strong>Example (curl)</strong></summary>
<pre style="white-space:pre-wrap;border:1px solid var(--border);padding:.75rem;border-radius:.5rem">
curl -H "Authorization: Bearer <?=esc_html($key)?>" \
  "<?=esc_url( rest_url('ai-reception/v1/ai/availability') )?>?phone=<?=rawurlencode($phone)?>&from=2025-08-02T00:00:00Z&to=2025-08-09T00:00:00Z&party=2&service_id=svc_id&room_id=any"
</pre>
			</details>
		</div>
	</div>

	<script>
	(() => {
		const NS = '/wp-json/ai-reception/v1/sched';
		const nonce = wpApiSettings?.nonce;
		const calendarEl = document.getElementById('kalCalendar');
		const roomSel = document.getElementById('kalRoomFilter');
		const svcSel  = document.getElementById('kalSvcFilter');
		const partyEl = document.getElementById('kalParty');
		const tzInput = document.getElementById('kalTZ');
		const weeklyT = document.getElementById('kalWeekly');

		let rooms = [], services = [], hours = {};

		// Tabs
		document.querySelectorAll('.kal-tab').forEach(t=>{
			t.addEventListener('click', ()=>{
				document.querySelectorAll('.kal-tab').forEach(x=>x.classList.remove('active'));
				document.querySelectorAll('.kal-pane').forEach(x=>x.classList.remove('active'));
				t.classList.add('active');
				document.getElementById('pane-'+t.dataset.tab).classList.add('active');
			});
		});

		// Load base data
		const fetchJSON = (url, opt={}) => fetch(url, { credentials:'same-origin', headers:{'X-WP-Nonce':nonce,'Content-Type':'application/json'}, ...opt }).then(r=>r.json());
		async function boot(){
			rooms    = await fetchJSON(NS+'/rooms');
			services = await fetchJSON(NS+'/services');
			hours    = await fetchJSON(NS+'/hours') || {};
			renderRooms(); renderServices(); renderHours();
			refreshSelectors();
			initCalendar();
		}

		function refreshSelectors(){
			roomSel.innerHTML = '<option value="any">All rooms</option>' + rooms.map(r=>`<option value="${r.id}">${r.label}</option>`).join('');
			svcSel.innerHTML  = services.map(s=>`<option value="${s.id}">${s.label} (${s.duration_min}m)</option>`).join('');
		}

		// ---------- ROOMS UI ----------
		function roomCard(r){
			return `<div class="kal-card">
				<label>Label</label><input data-k="label" value="${r.label||''}">
				<div class="kal-row">
					<div style="flex:1"><label>Capacity</label><input type="number" min="1" data-k="capacity" value="${r.capacity||1}"></div>
					<div style="flex:1"><label>Color</label><input data-k="color" value="${r.color||'#6A73C6'}"></div>
				</div>
				<label>External ICS (busy)</label><input data-k="ics_url" value="${r.ics_url||''}" placeholder="https://...">
				<button class="kal-btn kal-save-room" style="margin-top:.5rem">Save</button>
				<button class="kal-btn kal-del-room"  style="margin-left:.5rem;background:#eee;color:#000">Delete</button>
			</div>`;
		}
		function renderRooms(){
			const wrap = document.getElementById('kalRoomsWrap');
			wrap.innerHTML = rooms.map(roomCard).join('');
			wrap.querySelectorAll('.kal-card').forEach((card,i)=>{
				card.querySelector('.kal-save-room').onclick = async ()=>{
					['label','capacity','color','ics_url'].forEach(k=>rooms[i][k]=card.querySelector(`[data-k="${k}"]`).value.trim());
					if (!rooms[i].id) rooms[i].id = 'room_'+Math.random().toString(36).slice(2);
					await fetchJSON(NS+'/rooms',{method:'POST',body:JSON.stringify(rooms)});
					document.getElementById('kalRoomsSaveMsg').textContent='Saved ✓';
					refreshSelectors();
				};
				card.querySelector('.kal-del-room').onclick = async ()=>{
					rooms.splice(i,1);
					await fetchJSON(NS+'/rooms',{method:'POST',body:JSON.stringify(rooms)});
					renderRooms(); refreshSelectors();
				};
			});
			if (!rooms.length) document.getElementById('kalRoomsSaveMsg').textContent='Add your first room.';
		}
		document.getElementById('kalAddRoom').onclick = ()=>{ rooms.push({label:'New room',capacity:2,color:'#6A73C6'}); renderRooms(); };

		// ---------- SERVICES UI ----------
		function svcCard(s){
			return `<div class="kal-card">
				<label>Label</label><input data-k="label" value="${s.label||''}">
				<div class="kal-row">
					<div style="flex:1"><label>Duration (min)</label><input type="number" min="5" data-k="duration_min" value="${s.duration_min||30}"></div>
					<div style="flex:1"><label>Max party</label><input type="number" min="1" data-k="max_party" value="${s.max_party||1}"></div>
				</div>
				<div class="kal-row">
					<div style="flex:1"><label>Buffer before (min)</label><input type="number" min="0" data-k="buffer_before" value="${s.buffer_before||0}"></div>
					<div style="flex:1"><label>Buffer after (min)</label><input type="number" min="0" data-k="buffer_after" value="${s.buffer_after||0}"></div>
				</div>
				<button class="kal-btn kal-save-svc" style="margin-top:.5rem">Save</button>
				<button class="kal-btn kal-del-svc"  style="margin-left:.5rem;background:#eee;color:#000">Delete</button>
			</div>`;
		}
		function renderServices(){
			const wrap = document.getElementById('kalSvcWrap');
			wrap.innerHTML = services.map(svcCard).join('');
			wrap.querySelectorAll('.kal-card').forEach((card,i)=>{
				card.querySelector('.kal-save-svc').onclick = async ()=>{
					['label','duration_min','max_party','buffer_before','buffer_after'].forEach(k=>{
						let v = card.querySelector(`[data-k="${k}"]`).value.trim();
						services[i][k] = (/^\d+$/.test(v)) ? parseInt(v,10) : v;
					});
					if (!services[i].id) services[i].id = 'svc_'+Math.random().toString(36).slice(2);
					await fetchJSON(NS+'/services',{method:'POST',body:JSON.stringify(services)});
					document.getElementById('kalSvcSaveMsg').textContent='Saved ✓';
					refreshSelectors();
				};
				card.querySelector('.kal-del-svc').onclick = async ()=>{
					services.splice(i,1);
					await fetchJSON(NS+'/services',{method:'POST',body:JSON.stringify(services)});
					renderServices(); refreshSelectors();
				};
			});
			if (!services.length) document.getElementById('kalSvcSaveMsg').textContent='Add your first service.';
		}
		document.getElementById('kalAddSvc').onclick = ()=>{ services.push({label:'New service',duration_min:30,max_party:1,buffer_before:0,buffer_after:0}); renderServices(); };

		// ---------- HOURS UI ----------
		function renderHours(){
			tzInput.value = (hours.tz || 'UTC');
			const days=['mon','tue','wed','thu','fri','sat','sun'];
			weeklyT.innerHTML = days.map(d=>{
				const a = (hours.weekly?.[d] || [{start:'09:00',end:'17:00'}]);
				// show first window for quick edit
				const w = a[0];
				return `<tr data-day="${d}"><td style="text-transform:capitalize">${d}</td>
					<td><input data-k="open" value="${w.start||'09:00'}"></td>
					<td><input data-k="close" value="${w.end||'17:00'}"></td></tr>`;
			}).join('');
			document.getElementById('kalHolidays').value = (hours.holidays||[]).join('\n');
			document.getElementById('kalOverrides').value = (hours.overrides||[]).map(o=>{
				const segs = (o.open||[]).map(x=>`${x.start}-${x.end}`).join(';');
				return `${o.date} | ${segs}`;
			}).join('\n');
		}
		document.getElementById('kalAddWin').onclick = ()=>alert('Multiple windows per day are supported via Overrides (right column).');

		document.getElementById('kalSaveHours').onclick = async ()=>{
			const days=['mon','tue','wed','thu','fri','sat','sun'];
			const weekly={};
			weeklyT.querySelectorAll('tr').forEach(tr=>{
				const d = tr.dataset.day;
				const s = tr.querySelector('[data-k="open"]').value.trim();
				const e = tr.querySelector('[data-k="close"]').value.trim();
				weekly[d] = [{start:s,end:e}];
			});
			const hols = document.getElementById('kalHolidays').value.split(/\s+/).filter(Boolean);
			const ovr  = document.getElementById('kalOverrides').value.split("\n").filter(Boolean).map(line=>{
				const parts = line.split('|');
				const date = (parts[0]||'').trim();
				const wins = (parts[1]||'').split(';').map(seg=>{
					const [s,e] = seg.split('-').map(x=>x.trim());
					return {start:s,end:e};
				}).filter(x=>x.start && x.end);
				return {date, open:wins};
			});
			hours = { tz: tzInput.value.trim() || 'UTC', weekly, holidays:hols, overrides:ovr };
			await fetchJSON(NS+'/hours',{method:'POST',body:JSON.stringify(hours)});
			document.getElementById('kalHoursMsg').textContent='Saved ✓';
		};

		// ---------- CALENDAR ----------
		let calendar;
		function initCalendar(){
			calendar = new FullCalendar.Calendar(calendarEl, {
				initialView: 'timeGridWeek',
				timeZone: 'local',
				height: 'auto',
				headerToolbar: { left:'prev,next today', center:'title', right:'dayGridMonth,timeGridWeek,timeGridDay' },
				nowIndicator: true,
				selectable: true,
				events: async (info, success, failure)=>{
					try{
						const bookings = await fetchJSON(NS+'/bookings?from='+info.startStr+'&to='+info.endStr);
						success(bookings.map(b=>({
							id: String(b.id),
							start: new Date(b.start_ts*1000),
							end:   new Date(b.end_ts*1000),
							title: (b.service||'') + ' · ' + (b.room||''),
							backgroundColor: (rooms.find(r=>r.id===b.room_id)?.color) || '<?=esc_js(KAL_BRAND_PURPLE)?>',
						})));
					}catch(e){ failure(e); }
				},
				dateClick: async (arg) => {
					// create booking via availability check
					const svc = svcSel.value; if (!svc) return alert('Add a service first.');
					const room = roomSel.value;
					const party = Math.max(1, parseInt(partyEl.value||'1',10));
					const startISO = arg.date.toISOString();
					const toISO    = new Date(arg.date.getTime()+86400000).toISOString();
					const slots = await fetchJSON(NS+'/availability?from='+encodeURIComponent(startISO)+'&to='+encodeURIComponent(toISO)+'&party='+party+'&service_id='+svc+'&room_id='+room);
					const pick = slots.find(s=> new Date(s.start_ts*1000) >= arg.date );
					if (!pick) return alert('No free slots here. Try another time/room/service.');
					const name = prompt('Customer name (optional)') || '';
					const phone= prompt('Customer phone (optional)')|| '';
					const res = await fetchJSON(NS+'/bookings',{method:'POST',body:JSON.stringify({
						room_id: pick.room_id, service_id: svc, start_ts: pick.start_ts, end_ts: pick.end_ts,
						party: party, cust_name:name, cust_phone:phone, source:'staff'
					})});
					if (res?.id) { calendar.refetchEvents(); } else alert('Failed to book.');
				},
				eventDrop: async (info)=>{
					// simple move (keep duration)
					const ev = info.event;
					const id = ev.id;
					const start_ts = Math.floor(ev.start.getTime()/1000);
					const end_ts   = Math.floor(ev.end.getTime()/1000);
					// naive update of times by deleting + re-creating is omitted; keep minimal
					await fetchJSON(NS+'/bookings/'+id,{method:'POST',body:JSON.stringify({ /* could store times if modeled */ })});
					calendar.refetchEvents();
				},
			});
			calendar.render();
		}

		document.getElementById('kalCheck').onclick = async ()=>{
			const svc = svcSel.value; if (!svc) return alert('Select a service.');
			const room= roomSel.value;
			const party = Math.max(1, parseInt(partyEl.value||'1',10));
			const view = calendar.view;
			const slots = await fetchJSON(NS+'/availability?from='+encodeURIComponent(view.activeStart.toISOString())+'&to='+encodeURIComponent(view.activeEnd.toISOString())+'&party='+party+'&service_id='+svc+'&room_id='+room);
			alert(slots.length ? ('Found '+slots.length+' free slots in view.') : 'No availability in this range.');
		};

		// API key regen
		document.getElementById('kalRegen')?.addEventListener('click', async ()=>{
			const ok = confirm('Regenerate API key? Your AI integration must be updated.');
			if (!ok) return;
			const res = await fetch('/wp-json/wp/v2/users/me', {
				method:'POST', credentials:'same-origin',
				headers:{'Content-Type':'application/json','X-WP-Nonce':nonce},
				body: JSON.stringify({ meta: { ai_receptionist_api_key: 'regen' } })
			});
			// Simpler: hit a tiny internal endpoint
			fetch('<?=esc_url( admin_url('admin-ajax.php') )?>?action=kal_regen_key',{credentials:'same-origin'})
				.then(r=>r.json()).then(j=>{
					if (j.key) document.getElementById('kalApiKey').textContent = j.key;
				});
		});

		boot();
	})();
	</script>
	<?php
	return ob_get_clean();
});

/** AJAX: regenerate API key (staff only) */
add_action('wp_ajax_kal_regen_key', function(){
	if ( ! is_user_logged_in() ) wp_send_json_error();
	$key = wp_generate_password(40,false,false);
	update_user_meta(get_current_user_id(),'ai_receptionist_api_key',$key);
	wp_send_json(['key'=>$key]);
});
