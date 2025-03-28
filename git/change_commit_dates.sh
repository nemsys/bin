timestamp_format='%a %b %d %H:%M:%S %Y %z'

now=$(date +"${timestamp_format}")
new_date=${now}

usage()
{
	echo "usage: change_commit_date [[[-n ] [-i]] | [-h]]"
	echo "-n: use curent datetime"
	echo "-e: enter custom datetime (${timestamp_format})"
}

get_cmd_args()
{
	while [ "$1" != "" ]; do
		case $1 in
			-n | --now )
									new_date=${now}
									;;
			-i | --interactive )    interactive=1
									;;
			-h | --help )           usage
									exit
									;;
			* )                     usage
									exit 1
		esac
		shift
	done
}


get_user_data()
{
	echo -n "Enter timestamp (${now}): "
	read response

	if [ -n "$response" ]; then
		new_date=$response
	fi
}


get_user_data

echo "The commit date will be changed to: ${new_date}. Are you sure?[y]:"
read continue


if [ "$continue" == "y" ]; then
	# GIT_COMMITTER_DATE="Mon 20 Aug 2018 20:19:19 BST" git commit --amend --no-edit --date "Mon 20 Aug 2018 20:19:19 BST"
	GIT_COMMITTER_DATE=\""${new_date}"\" GIT_AUTHOR_DATE=\""${new_date}"\" git commit --amend --no-edit --date \""${new_date}"\"
else
	echo "Ok, no change will be made. Good Bye!"
fi